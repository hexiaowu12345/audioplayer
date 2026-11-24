import os
import sys
import shutil
from strings import get_str

# ================= 配置 FFmpeg 工具路径 =================
# Windows 默认路径（你 win_audioplayer.py 里的固定路径）
if sys.platform.startswith('win'):
    _DEFAULT_FFPLAY  = r"C:\终端\python\tools\AudioTools\ffplay.exe"
    _DEFAULT_FFPROBE = r"C:\终端\python\tools\AudioTools\ffprobe.exe"
else:
    _DEFAULT_FFPLAY  = None
    _DEFAULT_FFPROBE = None


def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


# 固定路径优先，PATH 兜底（找不到也不抛异常，交给 check_environment 友好提示）
FFPLAY_PATH = _first_existing([
    _DEFAULT_FFPLAY,
    shutil.which("ffplay"),
    shutil.which("mpv"),
])

FFPROBE_PATH = _first_existing([
    _DEFAULT_FFPROBE,
    shutil.which("ffprobe"),
])

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.wma', '.opus'}


def check_environment():
    """检查播放依赖，返回 (ok, errors, warnings)。

    - 没有 ffplay/mpv  -> 致命
    - 没有 ffprobe     -> 有 mutagen 时只警告（元数据改用 mutagen），否则致命
    缺依赖时不再抛 RuntimeError 直接崩掉。
    """
    errors, warnings = [], []
    if not FFPLAY_PATH:
        errors.append(get_str("err_no_ffplay"))
    if not FFPROBE_PATH:
        try:
            import mutagen  # noqa: F401
            warnings.append(get_str("warn_no_ffprobe_mutagen"))
        except Exception:
            errors.append(get_str("err_no_ffprobe"))
    return (not errors), errors, warnings


def print_environment_error(problems, title=None, footer=True):
    """统一打印依赖缺失 / 环境提醒。"""
    print()
    print("=" * 55)
    print(title or get_str("err_env_title"))
    print("=" * 55)
    for p in problems:
        print("  " + p)
    print()
    if footer:
        print(get_str("err_env_footer"))
    print("=" * 55)
    print()
