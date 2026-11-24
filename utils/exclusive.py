"""独占输出后端（三平台同一套设置和命令，实现机制不同）

    Android / Termux : termux-usb + usbfs 等时流（tools/usbsink.c）
                       —— 绕过 AudioFlinger，直接对 USB 声卡发包
    桌面 Linux/macOS : ALSA hw: 直通（mpv --ao=alsa --audio-device=alsa/hw:N,0）
                       —— 绕过 PulseAudio/PipeWire 混音，直接抢硬件
    Windows          : WASAPI 独占（mpv --ao=wasapi --audio-exclusive=yes）
                       —— 绕过 Windows 混音器（Audio Engine）

三者共用同一份设置与命令：
    usb on / usb off / usbprobe / usblist / usbuse / perfect / vol
设置存在 progress.json 的 settings 里（ex_enabled / ex_backend / ex_device ...）。
"""

import os
import re
import sys
import shutil
import subprocess

_TERMUX = 'termux'
_ALSA = 'alsa'
_WASAPI = 'wasapi'
_NONE = 'none'


# ==================== 后端识别 ====================
def _is_termux():
    return (sys.platform.startswith('android')
            or 'com.termux' in os.environ.get('PREFIX', ''))


def detect_backend():
    """当前机器该用哪个后端。"""
    if _is_termux():
        return _TERMUX
    if sys.platform.startswith('win'):
        return _WASAPI
    if sys.platform in ('linux', 'linux2'):
        return _ALSA
    if sys.platform == 'darwin':
        return _NONE          # macOS 要用 CoreAudio 独占，暂不支持
    return _NONE


def backend_note(backend=None):
    b = backend or detect_backend()
    return {
        _TERMUX: 'Termux/usbfs 独占（绕过 AudioFlinger）',
        _ALSA: 'ALSA hw: 直通（绕过 PulseAudio/PipeWire）',
        _WASAPI: 'WASAPI 独占（绕过 Windows 混音器）',
    }.get(b, '当前平台不支持独占输出')


def mpv_path():
    for name in ('mpv',):
        p = shutil.which(name)
        if p:
            return p
    return None


def available():
    """独占输出能不能用。返回 (bool, 原因)。"""
    b = detect_backend()
    if b == _TERMUX:
        from utils import usbdac
        return usbdac.usb_supported()
    if b in (_ALSA, _WASAPI):
        if not mpv_path():
            return False, '缺少 mpv（独占输出需要 mpv，不是 ffplay）'
        return True, 'ok'
    if sys.platform == 'darwin':
        return False, 'macOS 需要 CoreAudio 独占，暂未实现'
    return False, '当前平台不支持独占输出'


# ==================== 设置读写 ====================
def _st():
    from utils.storage import load_settings
    return load_settings()


def _patch(d):
    from utils.storage import save_settings
    return save_settings(d)


def get_config():
    """独占输出的当前配置；未开启或后端不可用返回 None。"""
    st = _st()
    if not st.get('ex_enabled'):
        return None
    ok, why = available()
    if not ok:
        return None
    b = st.get('ex_backend') or detect_backend()
    if b != detect_backend():      # 换了系统，旧后端设置不适用
        b = detect_backend()
    return {'enabled': True, 'backend': b,
            'device': st.get('ex_device') or '',
            'mixer': st.get('ex_mixer') or ''}


def save_config(enabled, device=None, mixer=None):
    patch = {'ex_enabled': bool(enabled), 'ex_backend': detect_backend()}
    if device is not None:
        patch['ex_device'] = device
    if mixer is not None:
        patch['ex_mixer'] = mixer
    return _patch(patch)


# ==================== 设备枚举（mpv 后端） ====================
_DEV_RE = re.compile(r"^\s*'([^']+)'\s*\(([^)]*)\)\s*$")


def list_mpv_devices(backend=None):
    """解析 mpv --audio-device=help，返回 [(id, 说明)]。"""
    b = backend or detect_backend()
    mpv = mpv_path()
    if not mpv or b not in (_ALSA, _WASAPI):
        return []
    try:
        out = subprocess.run([mpv, '--audio-device=help'], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return []
    devs = []
    for line in out.splitlines():
        m = _DEV_RE.match(line)
        if not m:
            continue
        did, desc = m.group(1), m.group(2).strip()
        if b == _ALSA:
            if did.startswith('alsa/') and ('hw:' in did or 'CARD=' in did):
                devs.append((did, desc))
        else:
            if did.startswith('wasapi/') or did == 'wasapi':
                devs.append((did, desc))
    return devs


_USB_HINTS = ('usb', 'dac', 'xmos', 'cx31993', 'speaker', 'headphone',
              '耳机', '扬声器', '数字输出', 'spdif')


def guess_device(devs):
    """在设备列表里猜哪一个是外接 DAC，猜不到就返回第一个。"""
    if not devs:
        return None
    for did, desc in devs:
        text = (did + ' ' + desc).lower()
        if any(h in text for h in _USB_HINTS):
            return did
    return devs[0][0]


# ==================== mpv 参数（非 Termux 后端） ====================
def mpv_args(perfect=False, rate=0, bits=0):
    """给 mpv 的独占参数。返回 [] 表示不加（保持默认输出）。"""
    cfg = get_config()
    if not cfg:
        return []
    b = cfg['backend']
    if b not in (_ALSA, _WASAPI):
        return []
    args = []
    if b == _ALSA:
        args.append('--ao=alsa')
        if cfg.get('device'):
            args.append('--audio-device=%s' % cfg['device'])
    else:
        args.append('--ao=wasapi')
        args.append('--audio-exclusive=yes')
        if cfg.get('device'):
            args.append('--audio-device=%s' % cfg['device'])
    if perfect:
        # 极致完美输出：位深强制 32bit 送出、采样率锁到源文件的采样率，避免重采样
        args.append('--audio-format=s32')
        if rate:
            args.append('--audio-samplerate=%d' % int(rate))
    return args


# ==================== 硬件音量 ====================
def hw_volume_supported():
    """(是否支持硬件音量, 说明)。Termux 走 usbsink，Linux 走 amixer，Windows 不支持。"""
    b = detect_backend()
    if b == _TERMUX:
        return True, 'UAC2 Feature Unit'
    if b == _ALSA:
        if shutil.which('amixer'):
            return True, 'ALSA mixer（amixer）'
        return False, '缺少 amixer（pkg install alsa-utils）'
    if b == _WASAPI:
        iface, why = _windows_endpoint()
        if iface is None:
            return False, why
        return True, 'Windows 端点音量（pycaw）'
    return False, '当前平台不支持硬件音量'


_WASAPI_VOL = {'ok': None, 'iface': None, 'why': ''}


def _windows_endpoint(cache=True):
    """取 Windows 默认播放设备的 IAudioEndpointVolume。

    需要 pycaw（pip install pycaw comtypes）。拿不到就返回 (None, 原因)。
    """
    if cache and _WASAPI_VOL['ok'] is not None:
        return _WASAPI_VOL['iface'], _WASAPI_VOL['why']
    if not sys.platform.startswith('win'):
        _WASAPI_VOL.update(ok=False, iface=None, why='不是 Windows 平台')
        return None, _WASAPI_VOL['why']
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except Exception as exc:
        why = '没装 pycaw（Windows 上执行 pip install pycaw comtypes）：%s' % exc
        _WASAPI_VOL.update(ok=False, iface=None, why=why)
        return None, why
    try:
        spk = AudioUtilities.GetSpeakers()
        ep = getattr(spk, 'EndpointVolume', None)      # pycaw >= 0.3 的写法
        if ep is None:                                  # 老版本 pycaw 的写法
            act = spk.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            ep = cast(act, POINTER(IAudioEndpointVolume))
        _WASAPI_VOL.update(ok=True, iface=ep, why='ok')
        return ep, 'ok'
    except Exception as exc:
        why = 'pycaw 初始化失败：%r' % exc
        _WASAPI_VOL.update(ok=False, iface=None, why=why)
        return None, why


def _alsa_card_of(device):
    """从 alsa/hw:1,0 或 alsa/hw:CARD=xxx 里抠出卡号。"""
    if not device:
        return None
    m = re.search(r'hw[=:]', device)
    tail = device[m.end():] if m else ''
    m2 = re.match(r'(\d+)', tail)
    return m2.group(1) if m2 else None


def alsa_volume_controls(card):
    """列出某张声卡上可调的播放音量控件名。"""
    try:
        out = subprocess.run(['amixer', '-c', str(card), 'scontrols'],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    names = []
    for line in out.splitlines():
        m = re.search(r"Simple mixer control '([^']+)'", line)
        if m:
            names.append(m.group(1))
    return names


def get_hw_volume():
    """读当前硬件音量百分比；读不到返回 None。"""
    b = detect_backend()
    if b == _WASAPI:
        iface, _why = _windows_endpoint()
        if iface is None:
            return None
        try:
            return int(round(iface.GetMasterVolumeLevelScalar() * 100))
        except Exception:
            return None
    return None


def set_hw_volume(pct, cfg=None):
    """硬件音量：目前能做的只有桌面 Linux 的 ALSA mixer。返回 (ok, 说明)。"""
    cfg = cfg or get_config() or {}
    b = detect_backend()
    if b == _TERMUX:
        return False, 'Termux 的硬件音量由 usbsink 直接改 UAC2，不走这里'
    if b == _WASAPI:
        iface, why = _windows_endpoint()
        if iface is None:
            return False, why
        pct = max(0, min(100, int(pct)))
        try:
            iface.SetMasterVolumeLevelScalar(pct / 100.0, None)
            return True, 'Windows 端点音量 -> %d%%' % pct
        except Exception as exc:
            return False, 'pycaw 设置失败：%r' % exc
    card = _alsa_card_of(cfg.get('device'))
    if card is None:
        return False, '没指定 ALSA 声卡（先用 usbuse 选设备）'
    ctl = cfg.get('mixer') or ''
    if not ctl:
        cands = alsa_volume_controls(card)
        for want in ('PCM', 'Master', 'Speaker', 'Headphone'):
            for c in cands:
                if c.lower() == want.lower():
                    ctl = c
                    break
            if ctl:
                break
        if not ctl and cands:
            ctl = cands[0]
    if not ctl:
        return False, '这张声卡没有可调音量控件'
    pct = max(0, min(100, int(pct)))
    try:
        r = subprocess.run(['amixer', '-c', card, 'sset', ctl, '%d%%' % pct],
                           capture_output=True, text=True, timeout=10)
        return (r.returncode == 0), ('%s -> %d%%' % (ctl, pct) if r.returncode == 0
                                     else (r.stderr or '').strip()[:80])
    except Exception as exc:
        return False, repr(exc)


# ==================== 摘要 ====================
def summary_line():
    from utils import usbdac
    cfg = get_config()
    if not cfg:
        return '系统默认输出（走系统混音器）'
    b = cfg['backend']
    if b == _TERMUX:
        return usbdac.summary_line()
    mode = '32bit 直送' if usbdac.perfect_enabled() else '位深跟随源文件'
    vol = ('硬件音量 %d%%' % usbdac.volume_percent()) if usbdac.get_volume_mode() == 'hw' \
          else ('软件音量 %d%%' % usbdac.volume_percent())
    return '%s %s / %s / %s' % (backend_note(b), cfg.get('device') or '默认设备', mode, vol)
