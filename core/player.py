import os
import sys
import time
import subprocess
from .config import FFPLAY_PATH
from utils.usbaudio import apply_to_env, extra_player_args
from utils import usbdac

# ==================== 平台差异：进程挂起 / 恢复 ====================
_IS_WINDOWS = sys.platform.startswith('win')

if _IS_WINDOWS:
    import ctypes
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _ntdll    = ctypes.WinDLL('ntdll')

    _kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    _kernel32.OpenProcess.restype  = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype  = ctypes.c_int
    _ntdll.NtSuspendProcess.argtypes = [ctypes.c_void_p]
    _ntdll.NtSuspendProcess.restype  = ctypes.c_long
    _ntdll.NtResumeProcess.argtypes  = [ctypes.c_void_p]
    _ntdll.NtResumeProcess.restype   = ctypes.c_long

    _PROCESS_SUSPEND_RESUME = 0x0800
    _CREATE_NO_WINDOW       = 0x08000000

    def _suspend_process(pid):
        h = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if not h: return False
        try:    return _ntdll.NtSuspendProcess(h) == 0
        finally: _kernel32.CloseHandle(h)

    def _resume_process(pid):
        h = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if not h: return False
        try:    return _ntdll.NtResumeProcess(h) == 0
        finally: _kernel32.CloseHandle(h)

    def _popen_kwargs():
        return {'creationflags': _CREATE_NO_WINDOW}
else:
    import signal as _signal

    def _suspend_process(pid):
        try: os.kill(pid, _signal.SIGSTOP); return True
        except Exception: return False

    def _resume_process(pid):
        try: os.kill(pid, _signal.SIGCONT); return True
        except Exception: return False

    def _popen_kwargs():
        return {}

class Player:
    def __init__(self, env):
        self.env = env
        self.proc = None
        self.start_offset = 0.0
        self._duration = 0.0
        self._started_at = None
        self._paused_at = None
        self._paused_accum = 0.0
        self._paused = False
        self.usb = None          # 非 None 时走 USB 独占输出（无 root）
        self._usb_died = False

    def _build_cmd(self, file_path):
        is_ffplay = 'ffplay' in os.path.basename(FFPLAY_PATH).lower()
        if is_ffplay:
            cmd = [FFPLAY_PATH, '-nodisp', '-autoexit', '-loglevel', 'error']
            if self.start_offset:
                cmd += ['-ss', f'{self.start_offset:.3f}']
            cmd.append(file_path)
        else:
            cmd = [FFPLAY_PATH, '--no-video', '--no-terminal', '--really-quiet']
            xargs = self._exclusive_args()
            if xargs is None:
                # 默认：AudioTrack/AAudio 直通，尽量少的处理
                try:
                    from utils import output as _out
                    cmd += _out.mpv_args(perfect=usbdac.perfect_enabled(),
                                         src_rate=getattr(self, '_src_rate', 0))
                except Exception:
                    pass
                cmd += extra_player_args()   # 老方案（root + ALSA，已废弃，兜底用）
            else:
                cmd += xargs                 # 新方案：ALSA hw: / WASAPI 独占
            if getattr(self, '_mpv_volume', None) is not None:
                cmd.append('--volume=%d' % int(self._mpv_volume))
            if self.start_offset:
                cmd += [f'--start=+{self.start_offset:.3f}']
            cmd.append(file_path)
        return cmd

    def _exclusive_args(self):
        """非 Termux 后端的独占参数；没开独占返回 None（表示可以用老方案兜底）。"""
        try:
            from utils import exclusive
            if not getattr(self, '_excl', None):
                return None
            return exclusive.mpv_args(perfect=usbdac.perfect_enabled(),
                                      rate=getattr(self, '_src_rate', 0))
        except Exception:
            return None

    def _errlog_path(self):
        for d in (os.environ.get('TMPDIR'), '/data/data/com.termux/files/usr/tmp',
                  os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
            if d and os.path.isdir(d):
                p = os.path.join(d, 'audioplayer_last.log')
                try:
                    with open(p, 'a'):
                        pass
                    return p
                except Exception:
                    continue
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'audioplayer_last.log')

    def last_error(self, n=2):
        """读本次播放进程的 stderr，返回最后 n 行（诊断用）。"""
        try:
            with open(self._errlog_path(), 'r', encoding='utf-8', errors='ignore') as f:
                lines = [x.strip() for x in f.read().strip().splitlines() if x.strip()]
            return ' ⏎ '.join(lines[-n:])
        except Exception:
            return ''

    def start(self, file_path, duration=0.0, offset=0.0):
        self.stop()
        self.start_offset = max(0.0, float(offset or 0.0))
        self._duration = float(duration or 0.0)
        self._started_at = time.time()
        self._paused_at = None
        self._paused_accum = 0.0
        self._paused = False

        # 独占输出：Termux 走 usbsink(usbfs)，Linux/Windows 走 mpv 的 ALSA/WASAPI 独占
        from utils import exclusive
        self._excl = exclusive.get_config()
        if self._excl and usbdac.usb_disabled_this_session():
            self._excl = None      # 本次运行已降级，别再每首都卡一秒
        self._mpv_volume = None
        self._src_rate = 0
        # 不管走哪条路，先把源采样率拿到（极致完美输出要用）
        try:
            from core.scanner import get_audio_metadata
            _m = get_audio_metadata(file_path) or {}
            self._src_rate = int(_m.get('raw_sample_rate') or 0)
        except Exception:
            self._src_rate = 0

        if self._excl:
            if self._excl['backend'] == 'termux':
                self._start_usb(file_path, {'device': self._excl.get('device')})
                return
            try:
                from core.scanner import get_audio_metadata
                meta = get_audio_metadata(file_path) or {}
                self._src_rate = int(meta.get('raw_sample_rate') or 0)
            except Exception:
                pass
            vol = usbdac.volume_percent()
            if usbdac.get_volume_mode() == 'hw':
                ok, msg = exclusive.set_hw_volume(vol, self._excl)
                if not ok:
                    self._mpv_volume = vol     # 硬件音量不可用 -> 退回软件音量
            else:
                self._mpv_volume = vol

        kwargs = {'env': apply_to_env(self.env), 'stdin': subprocess.DEVNULL}
        kwargs.update(_popen_kwargs())
        try:
            self._errlog = open(self._errlog_path(), 'w', encoding='utf-8', errors='ignore')
            kwargs['stderr'] = self._errlog
        except Exception:
            self._errlog = None
        self.proc = subprocess.Popen(self._build_cmd(file_path), **kwargs)

    def _start_usb(self, file_path, cfg):
        src_rate, src_bits = 0, 0
        try:
            from core.scanner import get_audio_metadata
            meta = get_audio_metadata(file_path) or {}
            src_rate = int(meta.get('raw_sample_rate') or 0)
            src_bits = int(str(meta.get('bits') or 0).split()[0] or 0)
            try:
                src_ch = int(str(meta.get('channels') or 2).split()[0])
            except Exception:
                src_ch = 2
            if src_ch == 1:
                print('  [USB] 单声道源：设备流接口固定 2 声道，会做一次上混')
        except Exception:
            pass
        prof = usbdac.get_profile(cfg.get('device'))
        # 强制抢占内核驱动时，必须用"这台设备真实探测过"的拓扑，不能用内置兜底
        if usbdac.get_force() and prof.get('_source') != 'cache':
            raise RuntimeError('还没探测过本机这枚设备的真实拓扑；'
                               '先 set-usb probe（或 set-usb test）成功一次，再 set-usb on force')
        rate, bits, notes = usbdac.choose_format(src_rate, src_bits, 2, profile=prof)
        for n in notes:
            print('  [USB] ' + n)
        lock = usbdac.get_rate_lock()
        if lock:
            print('  [USB] 已锁定 %d Hz 输出（设备时钟改不动时的折中办法）' % lock)
        # 采样率改不动？先借系统输出把设备时钟预热到目标值再抢
        if usbdac.get_force() and usbdac.warmup_enabled():
            ok_w, msg_w = usbdac.warmup_rate(rate)
            print('  [USB] ' + msg_w)

        try:
            self.usb = usbdac.UsbPlayback(
                file_path, rate=rate, bits=bits, ch=2,
                offset=self.start_offset, buf_sec=0.5,
                device=cfg.get('device'),
                volume_mode=usbdac.get_volume_mode(),
                volume_pct=usbdac.volume_percent(),
                profile=prof, force=usbdac.get_force())
            self.usb.start()
            self._usb_died = False
            # 起播自检：必须看到 "alt N 已激活" 才算真开流
            if not self.usb.wait_ready(2.5):
                rc = self.usb.sink_returncode()
                hint = usbdac.sink_exit_hint(rc) if rc is not None else '迟迟没开流'
                raise RuntimeError('%s　%s' % (hint, self.usb.failed_log(4)))
            self.proc = self.usb.dec      # 时长/退出码逻辑复用 ffmpeg 进程
            usbdac.note_usb_success()
            if self.usb.volume_mode == 'hw':
                sup = self.usb.sink.hw_volume_supported()
                if sup is False:
                    print('  [USB] 这枚小尾巴不提供硬件音量，本次按 0 dB 输出；'
                          '要调音量请用 vol sw')
            return
        except Exception as exc:
            try:
                if self.usb is not None:
                    self.usb.stop()
            except Exception:
                pass
            self.usb = None
            self.proc = None
            print('[USB 独占输出不可用，已回退系统输出] %s' % exc)
            usbdac.note_usb_failure(str(exc))

        kwargs = {'env': apply_to_env(self.env), 'stdin': subprocess.DEVNULL}
        kwargs.update(_popen_kwargs())
        try:
            self._errlog = open(self._errlog_path(), 'w', encoding='utf-8', errors='ignore')
            kwargs['stderr'] = self._errlog
        except Exception:
            self._errlog = None
        self.proc = subprocess.Popen(self._build_cmd(file_path), **kwargs)

    def usb_died(self):
        """中途断了吗（设备被拔 / 被系统抢回）。"""
        return bool(getattr(self, '_usb_died', False))

    def usb_abort(self):
        """放弃当前独占对象（不抛异常）。"""
        try:
            if self.usb is not None:
                self.usb.stop()
        except Exception:
            pass
        self.usb = None
        self.proc = None
        self._usb_died = False

    def usb_info(self):
        if self.usb is None:
            return None
        return self.usb.info()

    def set_volume(self, pct):
        """硬件音量实时生效；软件音量返回 False（下一首生效）。"""
        if self.usb is not None:
            return self.usb.set_volume(pct)
        try:
            from utils import exclusive
            if getattr(self, '_excl', None) and all(
                    [self._excl.get('backend') in ('alsa', 'wasapi')]):
                ok, _msg = exclusive.set_hw_volume(pct, self._excl)
                if ok:
                    return True
            if self.proc is not None and self.proc.poll() is None:
                self._mpv_volume = int(pct)   # 下次启动 mpv 时带上 --volume
        except Exception:
            pass
        return False

    def volume_mode(self):
        return None if self.usb is None else self.usb.volume_mode

    def hw_volume_supported(self):
        if self.usb is None:
            return None
        return self.usb.sink.hw_volume_supported()

    def is_playing(self):
        if self.usb is not None:
            ok = self.usb.alive()
            if not ok:
                self._usb_died = True
            return ok
        return self.proc is not None and self.proc.poll() is None

    def returncode(self):
        """播放进程的退出码（还在播放 / 未启动时返回 None）。"""
        if self.usb is not None:
            return self.usb.returncode()
        if self.proc is None:
            return None
        return self.proc.poll()

    def play_elapsed(self):
        """本次 start() 后实际经过的秒数（暂停时间不计入）。"""
        if self._started_at is None:
            return 0.0
        now = self._paused_at if self._paused else time.time()
        return max(0.0, now - self._started_at - self._paused_accum)

    def is_paused(self):
        return self._paused and self.is_playing()

    def current_position(self):
        if self._started_at is None:
            return self.start_offset
        now = self._paused_at if self._paused else time.time()
        elapsed = now - self._started_at - self._paused_accum
        return self.start_offset + max(0.0, elapsed)

    def duration(self):
        return self._duration

    def pause(self):
        if not self.is_playing() or self._paused: return False
        if self.usb is not None:
            if not self.usb.pause(): return False
        else:
            if not _suspend_process(self.proc.pid): return False
        self._paused = True
        self._paused_at = time.time()
        return True

    def resume(self):
        if not self.is_playing() or not self._paused: return False
        if self.usb is not None:
            if not self.usb.resume(): return False
        else:
            if not _resume_process(self.proc.pid): return False
        self._paused_accum += time.time() - self._paused_at
        self._paused_at = None
        self._paused = False
        return True

    def stop(self):
        if self.usb is not None:
            try:
                self.usb.stop()
            except Exception:
                pass
            self.usb = None
            self.proc = None
            self._started_at = None
            self._paused_at = None
            self._paused_accum = 0.0
            self._paused = False
            return
        if self.proc is None: return
        if self.proc.poll() is None:
            if self._paused:
                _resume_process(self.proc.pid)
                self._paused = False
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try: self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired: pass
        else:
            try: self.proc.wait()
            except Exception: pass
        self.proc = None
        try:
            if getattr(self, '_errlog', None):
                self._errlog.close()
        except Exception:
            pass
        self._errlog = None
        self._started_at = None
        self._paused_at = None
        self._paused_accum = 0.0
        self._paused = False