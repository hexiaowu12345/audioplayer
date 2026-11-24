"""USB 独占输出（无 root，走 termux-usb + usbfs 等时流）

链路：
    播放器进程
      └─ termux-usb -r -E -e usbsink /dev/bus/usb/001/002 play --rate 48000 --bits 24
            ├─ Android 弹「允许 termux 访问 CX31993+MAX97220 吗」→ 点确定
            └─ usbsink 拿 fd，CLAIMINTERFACE 抢下播放接口
                 （内核 snd-usb-audio 被顶掉，音频不再进 AudioFlinger）
                 → 设 UAC2 时钟采样率 → 等时 URB 每 1ms 送一包 PCM
    比特直通：ffmpeg 解码成裸 PCM → 管道 → usbsink → 小尾巴
不依赖 root，也不需要 /dev/snd 权限。
"""

import os
import sys
import atexit
import json
import time
import shutil
import signal
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_ROOT, 'usb_exclusive.json')   # 老版本用，仅作迁移来源

BIN_DIR = os.path.expanduser('~/audioplayer_usb/bin')
SINK = os.path.join(BIN_DIR, 'usbsink')
LAUNCH = os.path.join(BIN_DIR, 'usbsink-launch')
PROBE = os.path.join(BIN_DIR, 'usbprobe')
GUARD = os.path.join(_ROOT, 'tools', 'usb_guard.sh')
_SINK_PAT = 'audioplayer_usb/bin/usbsink'

# 进程内所有活着的独占进程：Python 正常退出/被 TERM 时统一收摊
_LIVE_SINKS = []
_handlers_installed = {'v': False, 'old': {}}


def _kill_all_sinks(reason='退出'):
    for sk in list(_LIVE_SINKS):
        try:
            sk.stop()
        except Exception:
            pass
    del _LIVE_SINKS[:]


def _on_exit_signal(signum, frame):
    """收到 TERM/HUP/INT 时先把 USB 还回去，再按原行为退出。"""
    _kill_all_sinks('信号 %s' % signum)
    old = _handlers_installed['old'].get(signum)
    try:
        signal.signal(signum, old if callable(old) else signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    except Exception:
        pass


def _install_exit_handlers():
    if _handlers_installed['v']:
        return
    _handlers_installed['v'] = True
    atexit.register(_kill_all_sinks, 'atexit')
    for name in ('SIGTERM', 'SIGHUP', 'SIGINT'):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            _handlers_installed['old'][sig] = signal.getsignal(sig)
            signal.signal(sig, _on_exit_signal)
        except Exception:
            pass


def sweep_sinks():
    """清掉上次崩溃残留的独占进程（只在本进程还没起 sink 时调用）。"""
    if not shutil.which('pkill'):
        return 0
    try:
        r = subprocess.run(['pkill', '-TERM', '-f', _SINK_PAT],
                           capture_output=True, text=True, timeout=8)
        return 0 if r.returncode != 0 else 1
    except Exception:
        return 0


def ensure_guard():
    """把 Termux 层面的看门狗拉起来（单实例，靠 usb_guard.sh 自己判断）。"""
    if not os.path.isfile(GUARD):
        return False
    try:
        if subprocess.run(['pgrep', '-f', 'usb_guard[.]sh'],
                          capture_output=True, timeout=5).returncode == 0:
            return True
        subprocess.Popen(['sh', GUARD], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except Exception:
        return False

# 实测描述符：alt -> 单包(1ms)最大字节数，对应 16/24/32bit
ALT_MPS = {2: 192, 3: 288, 4: 384}
RATES = (44100, 48000, 96000, 192000, 384000)


# ==================== 设置（统一存在 progress.json 的 settings 里） ====================
# 键名：
#   usb_exclusive   是否开启 USB 独占输出
#   usb_device      上次授权的设备路径
#   perfect_output  极致完美输出（源格式直送，不做任何处理）
#   volume_mode     hw = 用硬件音量（50 刻度） / sw = 用软件音量
#   volume_step     0~50，每格 2%，只在 hw 模式生效
VOLUME_STEPS = 50

# ==================== 平台能力（缺什么就降级什么，不抛异常） ====================
IS_WINDOWS = sys.platform.startswith('win')
IS_MACOS = sys.platform == 'darwin'
IS_ANDROID = (sys.platform.startswith('android')
              or 'com.termux' in os.environ.get('PREFIX', ''))
HAS_TERMUX_USB = bool(shutil.which('termux-usb'))
HAS_MKFIFO = hasattr(os, 'mkfifo')
_SIG_PAUSE = getattr(signal, 'SIGUSR1', None)     # Windows 没有，直接 None
_SIG_RESUME = getattr(signal, 'SIGUSR2', None)
_SIG_STOP = getattr(signal, 'SIGSTOP', None)
_SIG_CONT = getattr(signal, 'SIGCONT', None)


def platform_note():
    """给用户看的一句话平台说明。"""
    if IS_ANDROID and HAS_TERMUX_USB:
        return 'Termux（无 root，usbfs 独占可用）'
    if IS_WINDOWS:
        return 'Windows（USB 独占不可用，走系统混音输出）'
    if IS_MACOS:
        return 'macOS（USB 独占不可用，走系统混音输出）'
    if IS_ANDROID:
        return 'Android/Termux（缺 termux.usb 接口，独占不可用）'
    return 'Linux（USB 独占需要 termux-usb，非 Termux 环境不可用）'


def usb_supported():
    """当前平台能不能跑 USB 独占。返回 (bool, 原因)。"""
    if not HAS_TERMUX_USB:
        return False, '当前环境没有 termux-usb（需要 Termux + termux-api 包 + Termux:API 应用）'
    if not HAS_MKFIFO:
        return False, '当前平台没有 mkfifo，无法实时控制硬件音量'
    return True, 'ok'



def _settings():
    from utils.storage import load_settings
    return load_settings()


def _patch_settings(patch):
    from utils.storage import save_settings
    return save_settings(patch)


def load_config():
    """返回 {'enabled': bool, 'device': str}（兼容老键名 usb_*）。"""
    s = _settings()
    enabled = s.get('ex_enabled')
    if enabled is None:
        enabled = s.get('usb_exclusive')
    return {'enabled': bool(enabled),
            'device': s.get('ex_device') or s.get('usb_device') or ''}


def save_config(data):
    """只接受 enabled / device 两个键；新旧键名一起写，避免两处设置打架。"""
    patch = {}
    if 'enabled' in data:
        patch['usb_exclusive'] = bool(data['enabled'])
        patch['ex_enabled'] = bool(data['enabled'])
    if 'device' in data:
        patch['usb_device'] = data['device']
        patch['ex_device'] = data['device']
    return _patch_settings(patch)


def perfect_enabled():
    return bool(_settings().get('perfect_output'))


def set_perfect(on):
    return _patch_settings({'perfect_output': bool(on)})


def warmup_enabled():
    v = _settings().get('usb_warmup')
    return True if v is None else bool(v)


def set_warmup(on):
    return _patch_settings({'usb_warmup': bool(on)})


_WARMED = {'rate': 0}


def warmup_rate(rate, seconds=1.0):
    """借系统输出把 USB 声卡的时钟设成 rate，再让 usbsink 抢占。

    背景：内核 usbfs 不允许我们直接发 UAC2 采样率请求，
    但内核驱动自己可以。所以先用 mpv 走系统输出播一段"目标采样率的静音"，
    ALSA 就会把设备时钟设好；设备会记住这个时钟，随后我们抢过来按同一速率发数据。
    返回 (ok, 说明)。
    """
    mpv = shutil.which('mpv')
    if not mpv:
        return False, '缺少 mpv，无法预热采样率'
    rate = int(rate)
    if rate <= 0:
        return False, '采样率无效'
    if _WARMED['rate'] == rate:
        return True, '设备时钟已经预热过 %d Hz，跳过' % rate
    cmd = [mpv, '--no-video', '--no-terminal', '--really-quiet',
           '--audio-samplerate=%d' % rate, '--audio-channels=stereo',
           'av://lavfi:anullsrc=r=%d:cl=stereo' % rate]
    try:
        subprocess.run(cmd, timeout=max(0.4, float(seconds)),
                       stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    except Exception as exc:
        return False, '预热失败：%r' % exc
    time.sleep(0.6)       # 等 ALSA 彻底关掉设备（太短会抢不到，报 busy）
    _WARMED['rate'] = rate
    return True, '已用系统输出把设备时钟预热到 %d Hz' % rate


def get_rate_lock():
    """强制输出采样率（0 = 跟随源文件）。用来对付改不了时钟的设备。"""
    try:
        return int(_settings().get('usb_rate_lock') or 0)
    except Exception:
        return 0


def set_rate_lock(rate):
    rate = max(0, int(rate))
    _patch_settings({'usb_rate_lock': rate})
    return rate


def get_volume_mode():
    return 'sw' if _settings().get('volume_mode') == 'sw' else 'hw'


def set_volume_mode(mode):
    return _patch_settings({'volume_mode': 'sw' if mode == 'sw' else 'hw'})


def get_volume_step():
    try:
        v = int(_settings().get('volume_step', VOLUME_STEPS))
    except Exception:
        v = VOLUME_STEPS
    return max(0, min(VOLUME_STEPS, v))


def set_volume_step(step):
    step = max(0, min(VOLUME_STEPS, int(step)))
    _patch_settings({'volume_step': step})
    return step


def volume_percent(step=None):
    """50 格 -> 0~100%（每格 2%）。"""
    if step is None:
        step = get_volume_step()
    return int(round(step * 100.0 / VOLUME_STEPS))


def legacy_import():
    """老版本 usb_exclusive.json 的设置迁移到 progress.json（只做一次）。"""
    try:
        if not os.path.exists(CONFIG_FILE):
            return
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            old = json.load(f)
        if isinstance(old, dict) and old.get('device') and 'usb_device' not in _settings():
            patch = {'usb_device': old.get('device'),
                     'usb_exclusive': bool(old.get('enabled'))}
            _patch_settings(patch)
        os.remove(CONFIG_FILE)
    except Exception:
        pass


_NOTICE_ONCE = {'usb_unsupported': False}


def should_try_usb():
    """这次运行要不要试着走 USB 独占。

    设置里开着、而且当前平台真有 termux-usb 才试；
    平台不支持时只提示一次（避免每首歌刷一遍"回退系统输出"）。
    """
    legacy_import()
    cfg = load_config()
    if not cfg.get('enabled'):
        return False
    ok, why = usb_supported()
    if not ok:
        if not _NOTICE_ONCE['usb_unsupported']:
            _NOTICE_ONCE['usb_unsupported'] = True
            print('[USB] ' + why)
            print('[USB] 本次运行改用系统输出（独占设置仍保留，回到 Termux 自动生效）')
        return False
    return True


def get_forced_device():
    if not should_try_usb():
        return None
    return load_config()


def have_tools():
    ok, why = usb_supported()
    if not ok:
        return False, why
    missing = [n for n, f in (('usbprobe', PROBE), ('usbsink', SINK),
                              ('usbsink-launch', LAUNCH)) if not os.path.isfile(f)]
    if missing:
        return False, '缺少 %s（先跑 bash tools/build.sh）' % '、'.join(missing)
    return True, 'ok'


def list_devices():
    try:
        out = subprocess.run(['termux-usb', '-l'], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        data = json.loads(out) if out else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def pick_device(prefer=None):
    """选设备的优先级：
       1) 调用方指定的路径  2) 上次用的路径  3) 上次用的 VID:PID 现在挂在哪  4) 唯一设备
    """
    devs = list_devices()
    if not devs:
        return None
    st = _settings()
    for cand in (prefer, st.get('usb_device')):
        if cand and cand in devs:
            return cand
    want_id = st.get('usb_vidpid')
    if want_id:
        d = load_profiles()
        for path, key in (d.get('paths') or {}).items():
            if key == want_id and path in devs:
                return path
    return devs[0]


# ==================== 设备拓扑档案（探测一次，缓存复用） ====================
PROFILE_FILE = os.path.join(_ROOT, 'usb_devices.json')

# 没探测过的设备用这份兜底（就是这台 CX31993+MAX97220 的实测值）
DEFAULT_PROFILE = {
    'vid': '06cb', 'pid': '1594',
    'manufacturer': 'Synaptics', 'product': 'CX31993+MAX97220',
    'uac2': True, 'ac_iface': 0, 'as_iface': 1, 'ep_out': 1,
    'clock_id': 9, 'unit_id': 2, 'has_volume': True, 'capture_iface': 2,
    'alts': [
        {'alt': 1, 'bytes': 2, 'bits': 16, 'mps': 192, 'interval': 1},
        {'alt': 2, 'bytes': 3, 'bits': 24, 'mps': 288, 'interval': 1},
        {'alt': 3, 'bytes': 4, 'bits': 32, 'mps': 384, 'interval': 1},
    ],
}


def load_profiles():
    """读取拓扑缓存：{'profiles': {vid:pid: profile}, 'paths': {路径: vid:pid}}"""
    try:
        with open(PROFILE_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {'profiles': {}, 'paths': {}}
        d.setdefault('profiles', {})
        d.setdefault('paths', {})
        return d
    except Exception:
        return {'profiles': {}, 'paths': {}}


def save_profiles(d):
    try:
        with open(PROFILE_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=4)
        return True
    except Exception:
        return False


def valid_profile(prof):
    """档案必须能真的拿去配置硬件，否则宁可当作没探测过。

    之前就是缓存了一份 as_iface=-1/alts=[] 的空档案，
    播放器拿着假参数去操作设备，把系统音频搞哑了。
    """
    if not isinstance(prof, dict):
        return False
    try:
        if int(prof.get('as_iface', -1)) < 0:
            return False
        if int(prof.get('ep_out', -1)) < 0:
            return False
    except Exception:
        return False
    alts = prof.get('alts') or []
    if not any(a.get('mps') and (a.get('bits') or a.get('bytes')) for a in alts):
        return False
    return True


def profile_key(prof):
    vid, pid = prof.get('vid'), prof.get('pid')
    if vid in (None, '', 'ffffffff') or pid in (None, '', 'ffffffff'):
        return None
    return '%s:%s' % (vid.lower(), pid.lower())


def _probe_cmd(dev, mode='json'):
    """跑 usbprobe 的命令行 + 环境。

    关键：termux-usb 的 -e 只能接一条命令，任何额外参数都可能被它判成"多余参数"，
    所以模式一律用 USBPROBE_MODE 环境变量传，命令行只留设备路径。
    """
    env = dict(os.environ)
    env['USBPROBE_MODE'] = mode
    return ['termux-usb', '-r', '-E', '-e', PROBE, dev], env


def _explain_failure(text):
    """把 termux-usb / 后端的原始报错翻译成人话。"""
    low = (text or '').lower()
    if 'no such device' in low:
        return ('设备句柄失效：设备在列表里，但系统不让 open。'
                '多半是上一次独占断开时系统没放开，把小尾巴拔下来再插上就好；'
                '插上后先跑 usb test，成功再 usb on。')
    if 'permission denied' in low or '权限' in (text or ''):
        return '权限不足：请在弹窗里点【确定】（如果没弹窗，用 usb test 触发一次）'
    if 'too many arguments' in low:
        return '参数传递出错（内部问题，请看上面原始输出）'
    if 'busy' in low or '占用' in (text or ''):
        return '设备被别的程序占用：先 usb off / 退出其它播放器，再拔插一次'
    if not text:
        return '没有任何输出（termux-api 没响应？确认 Termux:API 应用已安装）'
    return text.strip().splitlines()[-1][:120]


def _profile_from_text(text, device=None, save=True):
    """从 usbprobe 的 stdout 里抠出 JSON 行，必要时写进缓存。"""
    import json as _json
    line = ''
    for ln in (text or '').splitlines():
        ln = ln.strip()
        if ln.startswith('{') and ln.endswith('}'):
            line = ln
    if not line:
        return None
    try:
        prof = _json.loads(line)
    except Exception:
        return None
    if not valid_profile(prof):
        return None
    if save:
        d = load_profiles()
        key = profile_key(prof)
        if key:
            d['profiles'][key] = prof
        if device:
            d['paths'][device] = key or device
        save_profiles(d)
    return prof


def probe_profile(device=None, timeout=90, save=True):
    """跑 usbprobe json 拿到拓扑，写进缓存。返回 (profile, 说明)。"""
    ok, why = have_tools()
    if not ok:
        return None, why
    dev = device or pick_device()
    if not dev:
        return None, '没有检测到 USB 设备'
    cmd, env = _probe_cmd(dev, 'json')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, '探测超时（可能没点确定）'
    text = ((r.stdout or '') + (r.stderr or '')).strip()
    line = ''
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith('{'):
            line = ln
            break
    if not line:
        return None, '探测失败：%s' % (text.splitlines()[-1] if text else '无输出')
    try:
        prof = json.loads(line)
    except Exception:
        return None, '探测输出无法解析：%s' % line[:80]
    key = profile_key(prof)
    if save:
        d = load_profiles()
        if key:
            d['profiles'][key] = prof
        d['paths'][dev] = key or dev
        save_profiles(d)
    return prof, 'ok'


def _tagged(prof, source):
    p = dict(prof)
    p['_source'] = source        # 'cache' = 真探测过；'default' = 内置兜底
    return p


def get_profile(device=None):
    """取设备拓扑：路径缓存 -> VID:PID 缓存 -> 兜底档案（带 _source 标记）。"""
    dev = device or pick_device()
    d = load_profiles()
    if dev:
        key = d['paths'].get(dev)
        if key and key in d['profiles'] and valid_profile(d['profiles'][key]):
            return _tagged(d['profiles'][key], 'cache')
    vidpid = _settings().get('usb_vidpid')
    if vidpid and vidpid in d['profiles'] and valid_profile(d['profiles'][vidpid]):
        return _tagged(d['profiles'][vidpid], 'cache')
    good = [v for v in d['profiles'].values() if valid_profile(v)]
    if len(good) == 1:
        return _tagged(good[0], 'cache')
    return _tagged(DEFAULT_PROFILE, 'default')


def _alt_of(profile, bits):
    """在档案的 alt 表里找对应位深的设置，找不到返回 None。"""
    want = bits // 8
    for a in (profile.get('alts') or []):
        if a.get('bits') == bits or a.get('bytes') == want:
            return a
    return None


def mps_for(profile, bits):
    a = _alt_of(profile, bits)
    if a and a.get('mps'):
        return int(a['mps'])
    return ALT_MPS.get(bits // 8, 0)


def max_rate_for(bits_bytes, ch=2, profile=None):
    mps = mps_for(profile or DEFAULT_PROFILE, bits_bytes * 8)
    if not mps:
        return 0
    return mps * 1000 // (ch * bits_bytes)


def fits(rate, bits, ch=2, profile=None):
    mps = mps_for(profile or DEFAULT_PROFILE, bits)
    if not mps or rate <= 0:
        return False
    return rate * ch * (bits // 8) <= mps * 1000


def choose_format(src_rate=0, src_bits=0, ch=2, perfect=None, profile=None):
    """按源文件选 (rate, bits)。

    perfect=False（普通独占）：位深跟源走（16 位就用 16 位 alt），超范围回落 48k。
    perfect=True（极致完美输出）：
        1) 采样率强制用源文件的采样率（绝不重采样，比特率也就等于源比特率）
        2) 位深强制 32 bit（float32 精度直送，中间不做任何截断）
        3) 若端点单包容量塞不下，才逐级降 32->24->16 并给出提示
    返回 (rate, bits, notes)；notes 是给用户看的说明列表。
    """
    notes = []
    if perfect is None:
        perfect = perfect_enabled()
    lock = get_rate_lock()
    rate = int(src_rate or 0)
    if lock:
        if rate and rate != lock:
            notes.append('采样率锁定 %d Hz（源 %d Hz，会轻微变速换音高）' % (lock, rate))
        rate = lock
    if rate <= 0:
        rate = 48000
        notes.append('读不到源采样率，按 48kHz 送出')

    if profile is None:
        profile = get_profile()
    if perfect:
        for bits in (32, 24, 16):
            if fits(rate, bits, ch, profile):
                if bits != 32:
                    notes.append('端点容量不足：%d bit 装不下 %d Hz，降到 %d bit'
                                 % (32, rate, bits))
                return rate, bits, notes
        # 连 16bit 都塞不进（比如 96k 以上），只能降采样率
        ceiling = max_rate_for(2, ch, profile) or 48000
        notes.append('设备端点最大只能跑 %d Hz，%d Hz 无法零处理输出，已降为 %d Hz'
                     % (ceiling, rate, ceiling))
        return ceiling, 16, notes

    bits = 16 if (src_bits or 0) <= 16 else 24
    ceiling = max_rate_for(bits // 8, ch, profile) or 48000
    if rate > ceiling:
        notes.append('设备单包上限 %d Hz，%d Hz 已回落 48kHz' % (ceiling, rate))
        rate = 48000 if ceiling >= 48000 else ceiling
    return rate, bits, notes


def request_permission(device=None, timeout=90, progress=None):
    """调 termux-usb 申请权限：用户点“确定”后返回 (True, 探测输出)。"""
    ok, why = have_tools()
    if not ok:
        return False, why
    dev = pick_device(device)
    if not dev:
        return False, '没有检测到 USB 设备（小尾巴插好了吗？）'
    if progress:
        progress('请在弹出的「允许 termux 访问 …」窗口点【确定】')
    cmd, env = _probe_cmd(dev, 'json')
    text = ''
    r = None
    # 设备句柄偶尔要等一下才放开，遇到 No such device 自动重试一次
    for attempt in (1, 2):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return False, '等待授权超时（可能没点确定）'
        text = ((r.stdout or '') + (r.stderr or '')).strip()
        bad = (r.returncode != 0 or 'No such device' in text
               or 'Device or resource busy' in text)
        if not bad:
            break
        if attempt == 1 and 'no such device' in text.lower():
            time.sleep(2.0)
            continue
        return False, _explain_failure(text)
    prof = _profile_from_text(text, dev, save=True)
    if not prof:
        return False, '授权成功但读不到拓扑：%s' % (text.splitlines()[-1] if text else '无输出')
    key = profile_key(prof) or ''
    save_config({'enabled': True, 'device': dev})
    _patch_settings({'usb_vidpid': key})
    line = ('设备 %s:%s %s（%s）' % (prof.get('vid'), prof.get('pid'),
                                    prof.get('product') or '未知',
                                    'UAC2' if prof.get('uac2') else 'UAC1'))
    return True, line


def probe(device=None, timeout=60):
    """只读探测当前设备拓扑（会缓存），返回 (ok, 人看的说明)。"""
    ok, why = have_tools()
    if not ok:
        return False, why
    dev = pick_device(device)
    if not dev:
        return False, '没有检测到 USB 设备'
    cmd, env = _probe_cmd(dev, 'json')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, '探测超时'
    text = ((r.stdout or '') + (r.stderr or '')).strip()
    prof = _profile_from_text(text, dev, save=True)
    if not prof:
        return False, _explain_failure(text)
    lines = [
        '设备      : %s:%s  %s' % (prof.get('vid'), prof.get('pid'), prof.get('product') or '未知'),
        '厂商      : %s' % (prof.get('manufacturer') or '未知'),
        '协议      : %s' % ('UAC2' if prof.get('uac2') else 'UAC1'),
        '控制接口  : %d    播放流接口: %d    端点: 0x%02x' % (
            prof.get('ac_iface', -1), prof.get('as_iface', -1), prof.get('ep_out', 0)),
        '时钟源    : %s    Feature Unit: %s（音量 %s）' % (
            prof.get('clock_id', -1), prof.get('unit_id', -1),
            '支持' if prof.get('has_volume') else '不支持'),
    ]
    for a in (prof.get('alts') or []):
        lines.append('alt %-2d     : %2d bit / 单包上限 %d 字节（约 %d Hz 上限）' % (
            a.get('alt', 0), a.get('bits', 0), a.get('mps', 0),
            int(a.get('mps', 0)) * 1000 // max(1, 2 * max(1, a.get('bytes', 2)))))
    return True, '\n'.join(lines)


class UsbSink(object):
    """一个 usbsink 进程 = 一路独占输出，stdin 收裸 PCM。"""

    def __init__(self, device=None, rate=48000, bits=24, ch=2, buf_sec=0.5, log=None,
                 volume_pct=100, profile=None, force=False):
        self.device = pick_device(device)
        self.force = bool(force)
        self.profile = profile or get_profile(self.device)
        self.rate = int(rate)
        self.bits = int(bits)
        self.ch = int(ch)
        self.buf_sec = float(buf_sec)
        self.proc = None
        self.log_path = log
        self._log = None
        self.ctl_path = None
        self.pid_path = None
        self.volume_pct = int(volume_pct)

    # ---- 控制 FIFO：播放器写音量，usbsink 实时应用（改的是小尾巴硬件音量） ----
    def _make_fifo(self):
        try:
            d = os.environ.get('TMPDIR') or '/tmp'
            path = os.path.join(d, 'usbsink_%d.ctl' % os.getpid())
            try:
                os.remove(path)
            except OSError:
                pass
            os.mkfifo(path, 0o600)
            return path
        except Exception:
            return None

    def set_volume(self, pct):
        """实时设定硬件音量（0~100）。写失败返回 False。"""
        self.volume_pct = max(0, min(100, int(pct)))
        if not self.ctl_path:
            return False
        for _ in range(10):
            try:
                fd = os.open(self.ctl_path, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    os.write(fd, bytes([ord('V'), self.volume_pct]))
                finally:
                    os.close(fd)
                return True
            except OSError:
                time.sleep(0.05)
        return False

    def hw_volume_supported(self):
        """从 usbsink 日志判断设备是否支持硬件音量。"""
        for line in self.tail_log(40):
            if '硬件音量可用' in line:
                return True
            if '未提供硬件音量控制' in line:
                return False
        return None

    def _cmd(self):
        # termux-usb 的 -e 只能接一个命令、不能带参数，
        # 所以参数走环境变量，由 usbsink-launch 转成 usbsink 的命令行。
        return ['termux-usb', '-r', '-E', '-e', LAUNCH, self.device]

    def _env(self):
        env = dict(os.environ)
        env.update({'USBSINK_MODE': 'play',
                    'USBSINK_RATE': str(self.rate),
                    'USBSINK_BITS': str(self.bits),
                    'USBSINK_CH': str(self.ch),
                    'USBSINK_BUF': str(self.buf_sec),
                    'USBSINK_VOL': str(self.volume_pct)})
        if self.ctl_path:
            env['USBSINK_CTL'] = self.ctl_path
        if self.pid_path:
            env['USBSINK_PIDFILE'] = self.pid_path
        env['USBSINK_TOPO'] = self.topo_args()
        env['USBSINK_WATCH'] = str(os.getpid())   # 本进程一没，usbsink 自己收摊
        if self.force:
            env['USBSINK_FORCE'] = '1'
        return env

    def topo_args(self):
        """把拓扑档案翻译成 usbsink 的命令行参数（写死拓扑的时代结束了）。"""
        p = self.profile or {}
        def alt(bits):
            a = _alt_of(p, bits) or {}
            return a.get('alt', -1), a.get('mps', 0)
        a16, m16 = alt(16)
        a24, m24 = alt(24)
        a32, m32 = alt(32)
        # force 时要把整块设备的驱动都请走：AC + AS + 录音/HID 接口一起断，
        # 只断 AS 会让 snd-usb-audio 半死（声卡在、PCM 没了），系统音频会卡住。
        ifaces = []
        for k in ('ac_iface', 'as_iface', 'capture_iface'):
            v = p.get(k, -1)
            if isinstance(v, int) and v >= 0 and v not in ifaces:
                ifaces.append(v)
        return ('--ac-iface %d --as-iface %d --ep 0x%02x --clock %d --unit %d '
                '--uac %d --has-vol %d --ifaces "%s" --cap-iface %d '
                '--alt16 %d --alt24 %d --alt32 %d '
                '--mps16 %d --mps24 %d --mps32 %d') % (
            p.get('ac_iface', 0), p.get('as_iface', 1), p.get('ep_out', 1),
            p.get('clock_id', 9), p.get('unit_id', 2),
            2 if p.get('uac2') else 1, 1 if p.get('has_volume') else 0,
            ','.join(str(x) for x in ifaces), p.get('capture_iface', -1),
            a16, a24, a32, m16, m24, m32)

    def start(self):
        ok, why = have_tools()
        if not ok:
            raise RuntimeError(why)
        if not self.device:
            raise RuntimeError('没有检测到 USB 设备')
        _install_exit_handlers()
        sweep_sinks()           # 顺手清掉上次崩溃残留（此时本进程还没起自己的 sink）
        ensure_guard()          # Termux 层面的看门狗
        if not self.ctl_path:
            self.ctl_path = self._make_fifo()
        if not self.pid_path:
            d = os.environ.get('TMPDIR') or '/tmp'
            self.pid_path = os.path.join(d, 'usbsink_%d.pid' % os.getpid())
        if self._log is None:
            self._log = open(self.log_path or os.path.join(_ROOT, 'usb_sink.log'),
                             'w', encoding='utf-8', errors='ignore')
        self.proc = subprocess.Popen(self._cmd(), stdin=subprocess.PIPE,
                                     stdout=self._log, stderr=self._log,
                                     bufsize=0, env=self._env())
        _LIVE_SINKS.append(self)
        return self.proc

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def pause(self):
        return self._sig(_SIG_PAUSE)

    def resume(self):
        return self._sig(_SIG_RESUME)

    def real_pid(self):
        """usbsink 自己的 pid（不是 termux-usb 的）。"""
        try:
            with open(self.pid_path, 'r', encoding='utf-8') as f:
                pid = int(f.read().strip())
            if pid > 0:
                return pid
        except Exception:
            pass
        return None

    def _sig(self, sig):
        if sig is None:          # 平台不支持该信号（如 Windows）
            return False
        if not self.alive():
            return False
        pid = self.real_pid()
        if pid is None:
            return False         # 宁可不动，也不要错发信号把链路打死
        try:
            os.kill(pid, sig)
            return True
        except Exception:
            return False

    def write(self, data):
        if not self.alive():
            return False
        try:
            self.proc.stdin.write(data)
            return True
        except Exception:
            return False

    def stop(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
                for _ in range(60):
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.05)
                if self.proc.poll() is None:
                    self.proc.kill()
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
        finally:
            if self._log:
                try:
                    self._log.close()
                except Exception:
                    pass
            self.proc = None
            self._log = None
            try:
                _LIVE_SINKS.remove(self)
            except ValueError:
                pass
            if self.ctl_path:
                try:
                    os.remove(self.ctl_path)
                except Exception:
                    pass
                self.ctl_path = None
            if self.pid_path:
                try:
                    os.remove(self.pid_path)
                except Exception:
                    pass
                self.pid_path = None

    def tail_log(self, n=6):
        try:
            with open(self.log_path or os.path.join(_ROOT, 'usb_sink.log'),
                      'r', encoding='utf-8', errors='ignore') as f:
                lines = f.read().strip().splitlines()
            return lines[-n:]
        except Exception:
            return []


# ==================== 解码 + 推送 ====================
_FFMPEG_NAMES = ('ffmpeg',)


def find_ffmpeg():
    for n in _FFMPEG_NAMES:
        p = shutil.which(n)
        if p:
            return p
    return None


def decoder_cmd(file_path, rate, bits, ch=2, offset=0.0, ffmpeg=None, gain=None):
    """ffmpeg 解码成裸 PCM 吐到 stdout（不重采样、不做任何多余处理）。

    gain=None 表示不动音量（硬件音量在 usbsink 里调）；
    gain=0.0~1.0 时加一个 volume 滤镜（软件音量，仍然是 float 内部计算）。
    """
    ff = ffmpeg or find_ffmpeg()
    if not ff:
        raise RuntimeError('缺少 ffmpeg（pkg install ffmpeg）')
    codec = {16: 'pcm_s16le', 24: 'pcm_s24le', 32: 'pcm_s32le'}[bits]
    cmd = [ff, '-hide_banner', '-loglevel', 'error', '-nostdin']
    if offset and offset > 0:
        cmd += ['-ss', '%.3f' % float(offset)]
    cmd += ['-i', file_path, '-vn', '-map', '0:a:0']
    if gain is not None and gain < 0.9999:
        cmd += ['-af', 'volume=%.6f' % max(0.0, float(gain))]
    cmd += ['-acodec', codec, '-f', 's%dle' % bits,
            '-ar', str(int(rate)), '-ac', str(int(ch)), '-']
    return cmd


def volume_gain(percent):
    """软件音量增益：按人耳等响曲线取平方（50% 感知差不多是一半响）。"""
    p = max(0.0, min(100.0, float(percent))) / 100.0
    return p * p


class UsbPlayback(object):
    """sink + ffmpeg 一根管子：ffmpeg 的 stdout 直接接到 sink 的 stdin。"""

    def __init__(self, file_path, rate=48000, bits=24, ch=2, offset=0.0,
                 buf_sec=0.5, device=None, log=None, volume_mode=None, volume_pct=None,
                 profile=None, force=False):
        self.file_path = file_path
        self.rate = int(rate)
        self.bits = int(bits)
        self.ch = int(ch)
        self.offset = float(offset or 0.0)
        self.volume_mode = volume_mode or get_volume_mode()
        self.volume_pct = volume_percent() if volume_pct is None else int(volume_pct)
        self.profile = profile or get_profile(device)
        # 设备压根没有音量控制时，硬件音量这条路直接不通，本首改走软件音量
        self.hw_volume_possible = bool(self.profile.get('has_volume', True))
        if self.volume_mode == 'hw' and not self.hw_volume_possible:
            self.volume_mode = 'sw'
        self.sink = UsbSink(device=device, rate=self.rate, bits=self.bits,
                            ch=self.ch, buf_sec=buf_sec, log=log,
                            volume_pct=self.volume_pct, profile=self.profile,
                            force=force)
        self.dec = None
        self._paused = False

    def sw_gain(self):
        """软件音量时需要送给 ffmpeg 的增益；硬件音量时返回 None。"""
        if self.volume_mode == 'hw' and self.volume_pct >= 0:
            return None
        return volume_gain(self.volume_pct)

    def set_volume(self, pct):
        """统一入口：硬件音量走 FIFO，软件音量只能下一首生效（当前流无法改）。"""
        self.volume_pct = max(0, min(100, int(pct)))
        if self.volume_mode == 'hw':
            return self.sink.set_volume(self.volume_pct)
        return False

    def start(self):
        self.sink.start()
        cmd = decoder_cmd(self.file_path, self.rate, self.bits, self.ch,
                          self.offset, gain=self.sw_gain())
        # ffmpeg 直接写 sink 的管道；父进程要把写端关掉，ffmpeg 退出时 sink 才能收到 EOF
        self.dec = subprocess.Popen(cmd, stdout=self.sink.proc.stdin,
                                    stdin=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        try:
            self.sink.proc.stdin.close()
        except Exception:
            pass
        return self

    def alive(self):
        """解码进程活着 + 独占进程活着，才算在播（sink 死了等同于播放结束）。"""
        return (self.dec is not None and self.dec.poll() is None
                and self.sink.alive())

    def wait_ready(self, timeout=2.5):
        """等 usbsink 真正把流开起来（日志里出现"alt N 已激活"）。

        以前只等"进程还没死"，结果 termux-usb 报错晚一两百毫秒，
        播放器会以为独占成功，接着 ffmpeg 写管道失败、整首秒退，
        连挂 5 首就把播放熔断给触发了。
        """
        deadline = time.time() + max(0.5, float(timeout))
        while time.time() < deadline:
            if not self.alive():
                return False
            for line in self.sink.tail_log(40):
                if '已激活' in line or '等时流' in line:
                    return True
            time.sleep(0.05)
        return self.alive()

    def failed_log(self, n=5):
        return ' | '.join(self.sink.tail_log(n))

    def sink_returncode(self):
        """独占进程自己的退出码（比 ffmpeg 那个 224 有意义得多）。"""
        try:
            if self.sink.proc is not None:
                return self.sink.proc.poll()
        except Exception:
            pass
        return None

    def returncode(self):
        return None if self.dec is None else self.dec.poll()

    def pause(self):
        if self._paused:
            return False
        ok = self.sink.pause()
        if self.dec and self.dec.poll() is None and _SIG_STOP is not None:
            try:
                os.kill(self.dec.pid, _SIG_STOP)
            except Exception:
                pass
        self._paused = ok
        return ok

    def resume(self):
        if not self._paused:
            return False
        ok = self.sink.resume()
        if self.dec and self.dec.poll() is None and _SIG_CONT is not None:
            try:
                os.kill(self.dec.pid, _SIG_CONT)
            except Exception:
                pass
        self._paused = False
        return ok

    def stop(self):
        for p in (self.dec, None):
            if p is None:
                continue
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass
        try:
            if self.dec:
                self.dec.wait(timeout=3)
        except Exception:
            pass
        self.sink.stop()
        self.dec = None

    def info(self):
        return {'rate': self.rate, 'bits': self.bits,
                'device': self.sink.device, 'file': self.file_path,
                'volume_mode': self.volume_mode, 'volume': self.volume_pct,
                'perfect': perfect_enabled()}

def summary_line():
    """给 info / status 用的一句话输出说明。"""
    cfg = load_config()
    if not (cfg.get('enabled') and cfg.get('device')):
        return '系统默认输出（AudioFlinger 混音）'
    bits = 'float32 精度・32bit 直送' if perfect_enabled() else '位深跟源文件'
    vol = ('硬件音量 %d%%' % volume_percent()) if get_volume_mode() == 'hw' \
          else ('软件音量 %d%%' % volume_percent())
    return 'USB 独占 %s / %s / %s' % (cfg['device'], bits, vol)


def get_force():
    return bool(_settings().get('ex_force'))


def set_force(on):
    return _patch_settings({'ex_force': bool(on)})


def sink_exit_hint(code):
    """把 usbsink 的退出码翻成人话。"""
    return {
        3: '没拿到设备 fd（授权没成功，先 usb test）',
        4: '和设备的格式协商失败（拓扑或采样率不对）',
        5: 'URB 提交失败（设备被占用或已拔出）',
        6: '设备不接受这个采样率（会回退系统输出）',
        7: '接口正被内核驱动占用：默认不抢占，要用 usb on force',
    }.get(code, '未知错误（退出码 %s）' % code)

# ==================== 独占失败降级（别让 USB 问题污染熔断计数） ====================
_USB_FAIL = {'streak': 0, 'disabled': False, 'why': ''}


def note_usb_failure(why=''):
    """独占启动失败了：连续两次就本次运行不再尝试，避免每首歌都卡一秒。"""
    _USB_FAIL['streak'] += 1
    _USB_FAIL['why'] = why or ''
    if _USB_FAIL['streak'] >= 2 and not _USB_FAIL['disabled']:
        _USB_FAIL['disabled'] = True
        print('  [USB] 独占连续失败 %d 次，本次运行不再尝试独占，全部走系统输出'
              % _USB_FAIL['streak'])
        if why:
            print('  [USB] 最后一次原因：%s' % why)
        print('  [USB] 修好后重开播放器即可（或用 set-usb panic 看恢复步骤）')


def note_usb_success():
    _USB_FAIL['streak'] = 0


def usb_disabled_this_session():
    return _USB_FAIL['disabled']


def disable_usb_session(reason=''):
    """本次运行不再尝试独占（不改设置，只影响本次）。"""
    _USB_FAIL['disabled'] = True
    _USB_FAIL['why'] = reason or _USB_FAIL.get('why', '')


def reset_usb_failure():
    _USB_FAIL.update(streak=0, disabled=False, why='')


def usb_failure_reason():
    return _USB_FAIL['why']

VERSION = '2026-11-17.13'


def version_line():
    """启动时打一行，避免"改了代码但进程还是旧的"这种误会。"""
    try:
        cfg = load_config()
        return ('usbdac %s  独占=%s  force=%s  采样率锁=%s'
                % (VERSION, '开' if cfg.get('enabled') else '关',
                   '是' if get_force() else '否',
                   ('%d Hz' % get_rate_lock()) if get_rate_lock() else '跟随源文件'))
    except Exception:
        return 'usbdac %s' % VERSION
