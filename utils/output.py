"""输出后端选择：AudioTrack/AAudio 直通（尽量少的音频处理）

为什么是它：
  USB 独占（usbfs）这条路在这台机器上被内核卡死（采样率改不动、驱动回不来），
  所以默认改走 Android 平台的音频通道 —— mpv 的 aaudio 输出（底层就是 AudioTrack/AAudio），
  不抢内核驱动、不影响系统音频，同时把"播放器自己额外的处理"降到最少。

处理最小化做了三件事：
  1) --ao=aaudio + low-latency 模式：交给 AAudio 直送 HAL，不经过额外缓冲层
  2) --audio-channels=stereo：不做上混/下混
  3) 极致完美输出打开时：
       --audio-format=float   AAudio 原生就是 float32，我们不转成 s16 再让它转回去
       --audio-samplerate=<源采样率>  让 HAL 按源的采样率开流
     （Android 混音器若强制 48k，这一步是否生效取决于 HAL，做不到也只会退化成系统重采样）

设置项：progress.json -> settings.output_mode = 'audiotrack' | 'auto'
"""

import shutil
import subprocess

MODE_AUDIOTRACK = 'audiotrack'
MODE_AUTO = 'auto'


def _settings():
    from utils.storage import load_settings
    return load_settings()


def _patch(d):
    from utils.storage import save_settings
    return save_settings(d)


def get_mode():
    return MODE_AUTO if _settings().get('output_mode') == MODE_AUTO else MODE_AUDIOTRACK


def set_mode(mode):
    m = MODE_AUTO if mode == MODE_AUTO else MODE_AUDIOTRACK
    _patch({'output_mode': m})
    return m


def is_audiotrack():
    return get_mode() == MODE_AUDIOTRACK


def mpv_path():
    return shutil.which('mpv')


def aaudio_available():
    """mpv 是否带 aaudio 输出。"""
    mpv = mpv_path()
    if not mpv:
        return False, '缺少 mpv'
    try:
        out = subprocess.run([mpv, '--ao=help'], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception as exc:
        return False, '探测 mpv 输出失败：%r' % exc
    if 'aaudio' in out:
        return True, 'ok'
    return False, '这个 mpv 没有 aaudio 输出'


def mpv_args(perfect=False, src_rate=0):
    """给 mpv 的"低处理"参数；auto 模式返回空列表。"""
    if not is_audiotrack():
        return []
    ok, _why = aaudio_available()
    if not ok:
        return []
    args = ['--ao=aaudio', '--aaudio-performance-mode=low-latency',
            '--audio-channels=stereo']
    if perfect:
        args.append('--audio-format=float')
        if src_rate:
            args.append('--audio-samplerate=%d' % int(src_rate))
    return args


def mode_note():
    if not is_audiotrack():
        return '系统默认输出（mpv 自动选择 AO）'
    ok, why = aaudio_available()
    if not ok:
        return 'AudioTrack/AAudio 不可用（%s），已退回默认输出' % why
    return 'AudioTrack/AAudio 直通（低延迟，最少处理）'


def summary_line():
    from utils import usbdac
    perfect = usbdac.perfect_enabled()
    tail = '极致完美输出（float32 + 源采样率）' if perfect else '位深/采样率交给系统'
    return '%s · %s' % (mode_note(), tail)
