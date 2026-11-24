import os
import sys
import queue
import threading
import time

# ---- Windows 控制台 ANSI 颜色支持 ----
def enable_windows_ansi():
    """在 Windows 10+ 上开启控制台对 ANSI 转义序列的支持。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if handle is None or handle == -1:
            return
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass

enable_windows_ansi()  # 启动时立即开启

try:
    import termios
except ImportError:
    termios = None

from strings import get_str
from utils.formatter import format_time
from utils.storage import (save_progress, load_progress, clear_progress,
                           save_scan_cache, load_scan_cache, clear_scan_cache)
from .registry import (COMMANDS, COMMAND_GROUP, NAMESPACES, PRIMARY_COMMANDS,
                       resolve_namespace, namespace_name, group_commands)
from .player import Player
from .scanner import (get_audio_metadata, print_pipeline_info, scan_audio_files,
                      get_audio_files)
from .config import check_environment, print_environment_error

INPUT_PROMPT = get_str("prompt")

# 连续失败多少首后触发熔断
MAX_FAIL_STREAK = 5


class PlaybackContext:
    def __init__(self):
        self.player = None
        self.index = 0
        self.total = 0
        self.files = []
        self.file_path = None
        self.meta = None
        self.paused = False
        self.offset = 0.0
        self.loop_mode = False        # 单曲循环
        self.list_loop_mode = False   # 列表循环
        self.random_mode = False
        self.fail_streak = 0          # 连续失败计数（供 info/状态展示）
        self.namespace = None         # 当前子集（play/pl/set/sys）
        self.playlist_name = '默认'   # 当前播放列表名
        self.default_files = []       # 默认歌单（首次扫描到的全集）

_cmd_queue = queue.Queue()
_input_thread = None

def _input_worker():
    while True:
        try:
            line = input(_current_prompt)
        except (EOFError, KeyboardInterrupt):
            _cmd_queue.put(None)
            return
        except Exception:
            _cmd_queue.put(None)
            return
        _cmd_queue.put(line)

def start_input_worker():
    global _input_thread
    if _input_thread is None or not _input_thread.is_alive():
        _input_thread = threading.Thread(target=_input_worker, name="stdin-reader", daemon=True)
        _input_thread.start()

def flush_input():
    try:
        if termios is not None:
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        else:
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
    except Exception:
        pass

def clear_queue():
    while not _cmd_queue.empty():
        try:
            _cmd_queue.get_nowait()
        except queue.Empty:
            break

# ==================== 提示符（跟随当前子集） ====================
_PROMPT_KEYS = {
    None: "prompt",
    'play': "prompt_play",
    'pl': "prompt_pl",
    'set': "prompt_set",
    'sys': "prompt_sys",
}
_current_prompt = get_str("prompt")


def set_prompt(ns):
    global _current_prompt
    _current_prompt = get_str(_PROMPT_KEYS.get(ns, "prompt"))


def normalize_input(raw):
    """只做两个全角符号的兼容：？-> ?，行首 ＋ -> +。其它字符一律不动。"""
    line = str(raw).replace('？', '?')
    stripped = line.lstrip()
    if stripped.startswith('＋'):
        idx = line.index('＋')
        line = line[:idx] + '+' + line[idx + 1:]
    return line


def _suggest(name, candidates):
    import difflib
    cands = [c for c in candidates if c]
    # 前缀优先：lst -> ls，而不是相似度更高的 last
    pref = [c for c in cands if name.startswith(c)]
    if pref:
        return max(pref, key=len)
    try:
        matches = difflib.get_close_matches(name, cands, n=1, cutoff=0.6)
    except Exception:
        return None
    return matches[0] if matches else None


def _all_command_words(primary_only=False):
    words = set(PRIMARY_COMMANDS) if primary_only else set(COMMANDS.keys())
    words.update(NAMESPACES.keys())
    for cn, aliases in NAMESPACES.values():
        words.add(cn)
        words.update(aliases)
    words.discard('')
    return sorted(w for w in words if w)


def _enter_namespace(ctx, ns):
    if ctx.namespace == ns:
        print(get_str("ns_already", name=namespace_name(ns)))
        return None
    ctx.namespace = ns
    set_prompt(ns)
    print(get_str("ns_enter", name=namespace_name(ns)))
    print(get_str("ns_hint", name=namespace_name(ns)))
    return None


def _leave_namespace(ctx):
    if not ctx.namespace:
        print(get_str("ns_none"))
        return None
    ctx.namespace = None
    set_prompt(None)
    print(get_str("ns_exit"))
    return None


def _run_group_command(ctx, ns, name, args):
    """在指定子集内执行命令，返回 (是否已处理, action)。"""
    if ns == 'pl':
        from module.playlist import cmd_playlist
        return True, cmd_playlist(ctx, [name] + args)

    entry = COMMANDS.get(name)
    if entry and COMMAND_GROUP.get(name) == ns:
        return True, entry[1](ctx, args)

    other = resolve_namespace(name)
    if other and not args:
        return True, _enter_namespace(ctx, other)
    return False, None


def dispatch(raw_line, ctx):
    line = normalize_input(raw_line).strip()
    if not line:
        entry = COMMANDS.get('')
        return entry[1](ctx, []) if entry else None

    parts = line.split()
    name = parts[0].lower()
    args = parts[1:]

    # ---- 1. 返回顶层 / 切换子集 ----
    if name in ('back', '..'):
        return _leave_namespace(ctx)
    if name == 'cd':
        if not args or args[0] in ('..', '~', '/'):
            return _leave_namespace(ctx)
        ns = resolve_namespace(args[0])
        if ns:
            return _enter_namespace(ctx, ns)
        print(get_str("ns_unknown", name=args[0]))
        return None

    # ---- 2. pl 无参数：保持旧行为，先列歌单、不进入子集 ----
    if name == 'pl' and not args:
        from module.playlist import cmd_playlist
        return cmd_playlist(ctx, [])

    # ---- 3. 父集-子命令：play-loop / 设置-usb / pl-add ----
    for sep in ('-', '－', '·'):
        if sep in name:
            head, _, tail = name.partition(sep)
            ns = resolve_namespace(head)
            if ns and tail:
                handled, action = _run_group_command(ctx, ns, tail, args)
                if handled:
                    return action

    # ---- 4. 父集 + 空格子命令：play loop / set usb on / 设置 usb ----
    ns = resolve_namespace(name)
    if ns:
        if not args:
            return _enter_namespace(ctx, ns)
        handled, action = _run_group_command(ctx, ns, args[0].lower(), args[1:])
        if handled:
            return action

    # ---- 5. 表层命令始终可用 ----
    entry = COMMANDS.get(name)
    if entry and COMMAND_GROUP.get(name) == 'basic':
        return entry[1](ctx, args)

    # ---- 6. 子集内直接敲命令（进入 play 后打 loop）----
    if ctx.namespace:
        handled, action = _run_group_command(ctx, ctx.namespace, name, args)
        if handled:
            return action

    # ---- 7. 其它命令：必须带分类信息，不再"裸名就能跑" ----
    if entry:
        grp = COMMAND_GROUP.get(name, 'basic')
        if grp != 'basic' and grp != ctx.namespace:
            if ctx.namespace:
                print('  ⚠️ 你现在在【%s】分类里，而「%s」属于【%s】分类' % (
                    namespace_name(ctx.namespace), name, namespace_name(grp)))
                print('     先 back 回顶层再敲，或者显式写成 %s-%s' % (grp, name))
            else:
                print('  ⚠️ 「%s」属于【%s】分类，顶层不能直接敲' % (name, namespace_name(grp)))
                print('     写法：%s-%s%s   或先输入 %s 进入分类' % (
                    grp, name, (' ' + ' '.join(args)) if args else '', grp))
            return None
        return entry[1](ctx, args)

    # ---- 8. 进度跳转 +30 / -10 ----
    if name and name[0] in '+-<>' and not name.startswith('-n'):
        from module.playback import cmd_forward
        return cmd_forward(ctx, [name])

    # ---- 9. 纠错提示：只提示，绝不自动执行 ----
    # 先在主命令名里找，找不到再看别名，避免提示出奇怪的别名
    hint = (_suggest(name, _all_command_words(primary_only=True))
            or _suggest(name, _all_command_words()))
    if hint:
        print(get_str("unknown_cmd_hint", name=name, hint=hint))
    else:
        print(get_str("unknown_cmd", name=name))
    return None


def wait_for_action(ctx, poll_interval=0.2):
    player = ctx.player
    while True:
        if not player.is_playing():
            return 'eof'
        try:
            line = _cmd_queue.get(timeout=poll_interval)
        except queue.Empty:
            continue
        if line is None:
            return 'quit'
        action = dispatch(line, ctx)
        if action:
            return action

# ==================== 核心播放引擎 ====================

import os
import sys
import queue
import threading
import time
import random


def _wait_after_fuse(ctx):
    """熔断后暂停自动推进，等待用户明确指令。"""
    print(get_str("fuse_wait_hint"))
    while True:
        try:
            line = _cmd_queue.get()
        except Exception:
            return 'quit'
        if line is None:
            return 'quit'
        action = dispatch(line, ctx)
        if action is None:
            continue
        if action == 'quit':
            return 'quit'
        if action in ('rescan', 'replay'):
            return action
        if isinstance(action, tuple) and action[0] in ('rescan', 'refresh'):
            return action
        if isinstance(action, tuple) and action[0] in ('next', 'last', 'switch',
                                                      'play_playlist', 'pl_reordered'):
            return action
        print(get_str("fuse_need_action"))


def _targets_from_opts(folder_paths, paths, recursive):
    """整理扫描目标 -> [(根路径, 是否递归)]。

    - 明确给了路径：按用户指定，默认只扫本层，-r 才递归
    - 没给路径：沿用启动时的目录，保持旧的递归全量行为
    """
    if paths:
        rec = bool(recursive) if recursive is not None else False
        return [(p, rec) for p in paths], True
    return [(p, True) for p in (folder_paths or [])], False


def _in_scope(path, targets):
    """判断某个文件是否落在扫描范围内。"""
    p = os.path.normpath(path)
    for root, rec in targets:
        root = os.path.normpath(root)
        if rec:
            if p == root or p.startswith(root + os.sep):
                return True
        elif os.path.dirname(p) == root:
            return True
    return False


def _rescan_media(ctx, files, folder_paths, player, paths=None, recursive=None):
    """完整扫描（重建缓存）：清空进度、停止播放，整库重来。

    返回 (index, start_offset)；扫描失败返回 None。
    """
    old_file = ctx.file_path
    try:
        old_pos = player.current_position()
    except Exception:
        old_pos = 0.0
    player.stop()
    clear_progress()

    targets, custom = _targets_from_opts(folder_paths, paths, recursive)
    if not targets:
        print(get_str("rescan_no_dir"))
        return None

    if custom:
        print(get_str("rescan_path_start", total=len(targets)))
        new_files = []
        for root, rec in targets:
            if not os.path.exists(root):
                print(get_str("path_not_exist", folder=root))
                continue
            print(get_str("scanning", folder=root))
            new_files.extend(get_audio_files(root, recursive=rec))
        new_files.sort()
    else:
        print(get_str("rescan_start"))
        new_files = scan_and_filter_audio([t[0] for t in targets])

    total = len(new_files)
    if total == 0:
        print(get_str("rescan_empty"))
        return None

    files[:] = new_files
    ctx.total = total
    ctx.files = files
    ctx.default_files = list(files)      # 重新扫描后默认歌单同步更新
    print(get_str("rescan_done", total=total))

    if old_file in files:
        print(get_str("rescan_restored"))
        return (files.index(old_file), old_pos)
    return (0, 0.0)


def _incremental_rescan(ctx, files, folder_paths, player, paths=None, recursive=None):
    """增量扫描（第二种扫描方式）：不动缓存、不打断播放。

    只对扫描范围做"目录列举 + 已存在性比对"：
      磁盘有、缓存没有  -> 追加
      缓存有、磁盘没有  -> 移除
    不解析任何音频元数据，所以比完整扫描快得多。

    返回 (index, start_offset)；失败返回 None。
    """
    old_file = ctx.file_path
    try:
        old_pos = player.current_position()
    except Exception:
        old_pos = 0.0

    if paths:
        targets, _ = _targets_from_opts(folder_paths, paths, recursive)
    elif folder_paths:
        # 没给路径：沿用启动目录递归比对（只列目录+判存在，不读元数据）
        targets = [(p, True) for p in folder_paths]
    else:
        # 启动目录未知：退化为按缓存中现存文件所在目录逐层比对
        dirs = sorted({os.path.dirname(f) for f in files})
        targets = [(d, False) for d in dirs]

    if not targets:
        print(get_str("rescan_no_dir"))
        return None

    print(get_str("incr_rescan_start", total=len(files)))

    disk = set()
    for root, rec in targets:
        if not os.path.isdir(root):
            if paths:
                print(get_str("path_not_exist", folder=root))
            continue                       # 目录被删掉，其下文件会在下面被移除
        for f in get_audio_files(root, recursive=rec):
            disk.add(f)

    kept, removed = [], []
    for f in files:
        if _in_scope(f, targets):
            if f in disk:
                kept.append(f)
            else:
                removed.append(f)
        else:
            kept.append(f)

    known = set(files)
    added = sorted(f for f in disk if f not in known)
    new_files = sorted(kept + added)

    if not new_files:
        print(get_str("rescan_empty"))
        return None

    disk_all = set(new_files)
    default_pl = getattr(ctx, 'playlist_name', '默认') or '默认'
    if default_pl == '默认':
        files[:] = new_files
    else:
        # 自定义歌单：只同步删除失效曲目，不把新扫到的歌硬塞进歌单
        files[:] = [f for f in files
                    if (not _in_scope(f, targets)) or f in disk]
    ctx.total = len(files)
    ctx.files = files

    # 默认歌单跟着增删同步，缓存直接落盘（不清空、不重写整库）
    defaults = [f for f in ctx.default_files
                if (not _in_scope(f, targets)) or f in disk]
    known_default = set(defaults)
    defaults.extend(f for f in added if f not in known_default)
    ctx.default_files = sorted(defaults)
    save_scan_cache(new_files)

    if added or removed:
        print(get_str("incr_rescan_done", added=len(added),
                      removed=len(removed), total=len(new_files)))
    else:
        print(get_str("incr_rescan_none", total=len(new_files)))

    if old_file in files:
        return (files.index(old_file), old_pos)
    return (0, 0.0)


def _random_index(total):
    if total <= 1:
        return 0
    return random.randint(0, total - 1)


def _await_playback_action(ctx, player, file_path, duration):
    """等当前曲目播完或用户下指令；seek 就地处理，不退出。"""
    while True:
        try:
            action = wait_for_action(ctx)
        except KeyboardInterrupt:
            action = 'quit'
        if isinstance(action, tuple) and action[0] == 'seek':
            delta = action[1]
            new_pos = max(0.0, player.current_position() + delta)
            if duration > 0 and new_pos >= duration - 0.3:
                print(get_str("seek_end"))
                return 'eof'
            print(get_str("seek_to", pos=format_time(new_pos)))
            player.start(file_path, duration=duration, offset=new_pos)
            continue
        return action


# ==================== 核心播放引擎 ====================

def run_playback(files, env, start_index=0, start_offset=0.0, folder_paths=None):
    total = len(files)
    ctx = PlaybackContext()
    ctx.total = total
    ctx.files = files
    ctx.default_files = list(files)      # 默认歌单
    player = Player(env)
    ctx.player = player
    index = start_index
    should_save_progress = False
    is_first_play = True
    fail_streak = 0
    fuse_paused = False
    skip_restart = False      # 增量扫描后保持当前播放，不重新 start

    try:
        while 0 <= index < total:
            action = None
            duration = 0.0
            failed = False

            if fuse_paused:
                # ---- 熔断恢复：先听用户的 ----
                fuse_paused = False
                fail_streak = 0
                action = _wait_after_fuse(ctx)
            elif skip_restart:
                # ---- 增量扫描命中当前曲目：接着听，不重开进程 ----
                skip_restart = False
                file_path = files[index]
                ctx.file_path = file_path
                duration = (ctx.meta or {}).get('duration', 0.0)
                action = _await_playback_action(ctx, player, file_path, duration)
            else:
                flush_input()
                clear_queue()
                file_path = files[index]
                ctx.file_path = file_path
                meta = get_audio_metadata(file_path)

                if not meta:
                    name = os.path.basename(file_path)
                    print(get_str("skip_file", file=name))
                    fail_streak += 1
                    ctx.fail_streak = fail_streak
                    print(get_str("play_fail", count=fail_streak,
                                  max=MAX_FAIL_STREAK, file=name))
                    if fail_streak >= MAX_FAIL_STREAK:
                        print(get_str("fuse_tripped", max=MAX_FAIL_STREAK))
                        fuse_paused = True
                        continue          # 停在失败这首，等用户指令
                    index += 1
                    continue

                ctx.index = index
                ctx.meta = meta
                ctx.paused = False
                duration = meta.get('duration', 0.0)

                play_offset = start_offset if (index == start_index and is_first_play) else 0.0
                is_first_play = False
                if duration > 0 and play_offset >= duration - 3.0:
                    play_offset = 0.0
                ctx.offset = play_offset
                print(get_str("playing", index=index + 1, total=total,
                              title=meta['title'], artist=meta['artist']))
                print_pipeline_info(meta)

                try:
                    player.start(file_path, duration=duration, offset=play_offset)
                except Exception as exc:
                    name = os.path.basename(file_path)
                    print(get_str("play_start_error", err=exc))
                    fail_streak += 1
                    ctx.fail_streak = fail_streak
                    print(get_str("play_fail", count=fail_streak,
                                  max=MAX_FAIL_STREAK, file=name))
                    if fail_streak >= MAX_FAIL_STREAK:
                        print(get_str("fuse_tripped", max=MAX_FAIL_STREAK))
                        fuse_paused = True
                        continue          # 停在失败这首，等用户指令
                    index += 1
                    continue

                action = _await_playback_action(ctx, player, file_path, duration)

                played = player.play_elapsed()
                rc = player.returncode()
                # 失败判定：只针对自然结束(eof)且瞬间退出的情况，
                # 用户按 next/last/switch/replay 一律不算失败。
                if action == 'eof':
                    if played < 1.0:
                        # 明确很短的音频(<=2s)正常播完不算失败；时长未知(0)
                        # 或秒退说明文件/解码有问题
                        failed = not (0 < duration <= 2.0)
                    elif rc not in (0, None):
                        failed = True           # 进程异常退出

            # ==================== 动作处理 ====================
            if action == 'quit':
                should_save_progress = True
                break

            if isinstance(action, tuple) and action[0] == 'switch':
                index = action[1]
                fail_streak = 0
                ctx.fail_streak = 0
                should_save_progress = False
                continue

            if isinstance(action, tuple) and action[0] == 'next':
                step = action[1]
                index = _random_index(total) if ctx.random_mode else index + step
                fail_streak = 0
                ctx.fail_streak = 0
                continue

            if isinstance(action, tuple) and action[0] == 'last':
                step = action[1]
                index = _random_index(total) if ctx.random_mode else max(0, index - step)
                fail_streak = 0
                ctx.fail_streak = 0
                continue

            if isinstance(action, tuple) and action[0] == 'pl_reordered':
                name = action[1]
                cur_pl = getattr(ctx, 'playlist_name', '默认') or '默认'
                if cur_pl == name:
                    # 改的就是正在放的歌单：把内存列表同步成新顺序
                    from utils.playlists import get_tracks
                    tracks = get_tracks(name)
                    if tracks:
                        cur_file = ctx.file_path
                        files[:] = tracks
                        total = len(files)
                        ctx.files = files
                        ctx.total = total
                        index = files.index(cur_file) if cur_file in files else 0
                        ctx.index = index
                try:
                    alive = player.is_playing()
                except Exception:
                    alive = False
                if alive:
                    skip_restart = True          # 顺序变了，歌别断
                else:
                    is_first_play = True
                    should_save_progress = False
                continue

            if isinstance(action, tuple) and action[0] == 'play_playlist':
                from utils.playlists import get_tracks
                name = action[1]
                tracks = get_tracks(name)
                if not tracks:
                    print(get_str("pl_empty", name=name))
                    continue
                files[:] = tracks
                total = len(files)
                ctx.files = files
                ctx.total = total
                index = 0
                start_offset = 0.0
                is_first_play = True
                should_save_progress = False
                fail_streak = 0
                ctx.fail_streak = 0
                ctx.playlist_name = name
                print(get_str("pl_play", name=name, total=total))
                continue

            if isinstance(action, tuple) and action[0] == 'play_default':
                if not ctx.default_files:
                    print(get_str("pl_default_empty"))
                    continue
                files[:] = ctx.default_files
                total = len(files)
                ctx.files = files
                ctx.total = total
                index = 0
                start_offset = 0.0
                is_first_play = True
                should_save_progress = False
                fail_streak = 0
                ctx.fail_streak = 0
                ctx.playlist_name = '默认'
                print(get_str("pl_play", name='默认', total=total))
                continue

            if action == 'rescan' or (isinstance(action, tuple) and action[0] in ('rescan', 'refresh')):
                if isinstance(action, tuple):
                    opts = action[1] if len(action) > 1 and isinstance(action[1], dict) else {}
                    incremental = (action[0] == 'refresh') or bool(opts.get('incremental'))
                else:
                    opts = {}
                    incremental = False
                scan_paths = opts.get('paths') or None
                scan_recursive = opts.get('recursive')

                if incremental:
                    # 第二种扫描：保留缓存，只增删差异，不打断当前播放
                    cur_file = ctx.file_path
                    result = _incremental_rescan(ctx, files, folder_paths, player,
                                                 scan_paths, scan_recursive)
                    if result is None:
                        break
                    index = result[0]
                    total = len(files)
                    ctx.index = index
                    fail_streak = 0
                    ctx.fail_streak = 0
                    if cur_file and cur_file in files and ctx.meta:
                        skip_restart = True          # 接着播，不重开
                    else:
                        is_first_play = True
                        should_save_progress = False
                    continue

                result = _rescan_media(ctx, files, folder_paths, player,
                                       scan_paths, scan_recursive)
                if result is None:
                    break
                index, start_offset = result
                total = len(files)
                is_first_play = True
                should_save_progress = False
                fail_streak = 0
                ctx.fail_streak = 0
                continue

            # ---- 独占中途断开：不算播放失败，换系统输出重放这首 ----
            if action == 'eof' and getattr(player, 'usb_died', None) and player.usb_died():
                try:
                    from utils import usbdac
                    print('  [USB] 独占中途断开（设备被拔或系统收回），这首改用系统输出重放')
                    usbdac.note_usb_failure('独占中途断开')
                    player.usb_abort()
                except Exception:
                    pass
                is_first_play = True
                should_save_progress = False
                continue

            # ---- 熔断计数 ----
            if failed:
                fail_streak += 1
                ctx.fail_streak = fail_streak
                name = os.path.basename(ctx.file_path or '')
                print(get_str("play_fail", count=fail_streak,
                              max=MAX_FAIL_STREAK, file=name))
                # 播放进程秒退时把它自己的报错翻出来（以前被 --really-quiet 吞掉了）
                err = ''
                try:
                    err = player.last_error(2)
                except Exception:
                    err = ''
                if err:
                    print('  ↳ 播放器报错: ' + err[:200])

                if fail_streak >= MAX_FAIL_STREAK:
                    print(get_str("fuse_tripped", max=MAX_FAIL_STREAK))
                    try:
                        from utils import usbdac
                        why = usbdac.usb_failure_reason()
                        if why or usbdac.usb_disabled_this_session():
                            print('  ⚠️ 这一轮失败和 USB 独占有关：%s' % (why or '独占启动失败'))
                            print('     独占问题不算文件损坏，输入 set-usb panic 可一键停独占')
                            print('     然后按提示恢复系统音频（多数情况拔插一次小尾巴就够）')
                            print('     要继续听歌：输入 set-usb off 再 replay')
                    except Exception:
                        pass
                    fuse_paused = True
                    continue          # 停在失败这首，等用户指令
            else:
                fail_streak = 0
                ctx.fail_streak = 0

            # ---- 自动切歌 ----
            if action == 'eof':
                if ctx.random_mode:
                    index = _random_index(total)
                elif ctx.loop_mode:
                    pass
                elif ctx.list_loop_mode:
                    index += 1
                    if index >= total:
                        index = 0
                        print(get_str("list_loop_restart"))
                else:
                    index += 1
            elif action == 'replay':
                pass
            else:
                index += 1

    finally:
        pos_to_save = player.current_position()
        player.stop()
        if should_save_progress and ctx.file_path:
            save_progress(ctx.file_path, pos_to_save)
            print(get_str("quit_msg"))
        else:
            clear_progress()
        print(get_str("finished"))

# ==================== 扫描与过滤辅助函数 ====================

def _read_answer(prompt_text):
    """读取一条用户答复。

    交互线程已启动时从命令队列取，避免和 stdin 读取线程抢输入；
    未启动时退化为普通 input()。
    """
    if _input_thread is not None and _input_thread.is_alive():
        sys.stdout.write(prompt_text)
        sys.stdout.flush()
        try:
            line = _cmd_queue.get()
        except Exception:
            return ''
        if line is None:
            return ''
        return str(line).strip()
    try:
        return input(prompt_text).strip()
    except (EOFError, KeyboardInterrupt):
        return ''


def scan_and_filter_audio(folder_paths):
    """扫描并可选过滤音频，返回过滤后的列表"""
    files = scan_audio_files(folder_paths)
    if not files:
        return []

    total = len(files)
    user_input = _read_answer(get_str("filter_prompt", total=total)).lower()
    if user_input == '':
        print(get_str("filtering"))
        valid_files = []
        for idx, f in enumerate(files):
            # 修复进度条：加上 \033[K 清空整行残留字符，并缩短文件名长度避免换行
            name = os.path.basename(f)[:15]
            sys.stdout.write(f"\r\033[K检查中: [{idx+1}/{total}] {name}")
            sys.stdout.flush()
            meta = get_audio_metadata(f)
            if meta and meta.get('duration', 0) >= 15.0:
                valid_files.append(f)
        print()  # 过滤结束后换行，避免覆盖后续输出
        print(get_str("filter_done", valid=len(valid_files), filtered=total-len(valid_files)))
        return valid_files
    else:
        print(get_str("filter_cancelled"))
        return files

# ==================== 启动入口 ====================

def _usb_autostart():
    """启动时处理保存下来的 USB 独占状态。

    规则（按用户要求）：
      - 上次关闭了 -> 什么都不做
      - 开着但没插小尾巴 -> 不申请，只提示，插上后播放时会自动申请
      - 开着且插着 -> 立刻申请授权（授权过一次就不再弹窗）
    """
    try:
        from utils import usbdac
        from utils import exclusive
        print('  [USB] ' + usbdac.version_line())
        cfg = exclusive.get_config()
        if not cfg:
            # 没开独占：顺手看看有没有上次崩溃留下的独占进程占着声卡
            try:
                if usbdac.sweep_sinks():
                    print('  [安全] 清掉了上次残留的独占进程，USB 已还给系统')
            except Exception:
                pass
            return
        # 非 Termux 后端（ALSA hw: / WASAPI）：不需要弹授权，报告一句就行
        if cfg['backend'] != 'termux':
            print('  [独占] %s -> %s' % (exclusive.backend_note(),
                                        cfg.get('device') or '默认设备'))
            return
        if not usbdac.get_force():
            print('  [USB] 注意：独占已开但没开 force。Android 会一直占着声卡，')
            print('        这种状态下播放会回退系统输出（等于没独占）。')
            print('        要真独占：set-usb off 然后 set-usb on')
        ok, why = usbdac.usb_supported()
        if not ok:
            return
        ok, why = usbdac.have_tools()
        if not ok:
            print('  [USB] 已开启独占，但' + why)
            return
        devs = usbdac.list_devices()
        if not devs:
            print('  [USB] 已开启独占，但当前没插小尾巴：本次运行先用系统输出，'
                  '插上后重开播放器即可')
            usbdac.disable_usb_session('设备未插入')
            return
        if cfg.get('device') and cfg['device'] != devs[0]:
            usbdac.save_config({'enabled': True, 'device': devs[0]})
        print('  [USB] 正在确认独占授权…（若弹窗请点【确定】）')
        ok, text = usbdac.request_permission(devs[0])
        if ok:
            print('  [USB] 独占就绪：%s' % devs[0])
        else:
            print('  [USB] 授权没成功：%s' % text)
            print('  [USB] 本次播放将自动回退系统输出；可用 usb off 关掉独占')
    except Exception as exc:
        print('  [USB] 初始化异常：%s' % exc)


def start_playback(folder_paths):
    if isinstance(folder_paths, str):
        folder_paths = [folder_paths]

    # 0. 依赖检查（缺 ffplay/mpv/ffprobe 时给出友好提示而不是直接崩）
    ok, errors, warnings = check_environment()
    if warnings:
        print_environment_error(warnings, title=get_str("warn_env_title"), footer=False)
    if not ok:
        print_environment_error(errors)
        return

    # 1. 优先加载缓存
    files = load_scan_cache()
    if files:
        # 跨平台/换设备后缓存里可能全是从别处带来的失效路径：先筛一遍
        alive = [f for f in files if os.path.exists(f)]
        if len(alive) < len(files):
            gone = len(files) - len(alive)
            if not alive or len(alive) * 5 < len(files) * 4:
                print("  ⚠️ 缓存里 %d 首路径已失效（多半是别的系统/设备留下的），重新扫描" % gone)
                files = None
            else:
                print("  ⚠️ 跳过 %d 首已不存在的文件" % gone)
                files = alive
    if files:
        print(get_str("loading_cache", total=len(files)))
    else:
        print(get_str("cache_missing"))
        files = scan_and_filter_audio(folder_paths)
        if files:
            save_scan_cache(files)

    if not files:
        print(get_str("no_audio"))
        return

    total = len(files)
    print(get_str("found_audio", total=total))

    # 2. 读取上次播放进度
    last_file, last_pos = load_progress()
    resume_index = 0
    resume_offset = 0.0
    if last_file and last_file in files:
        resume_index = files.index(last_file)
        resume_offset = last_pos
        print(get_str("resume_play", time=format_time(last_pos)))

    # 3. 环境配置
    is_termux = 'com.termux' in os.environ.get('PREFIX', '')
    env = os.environ.copy()
    if is_termux:
        env['SDL_AUDIODRIVER'] = 'opensl'

    # 3.55 输出通道说明（默认 AudioTrack/AAudio 低处理直通）
    try:
        from utils import output as _out
        print('  [输出] ' + _out.summary_line())
    except Exception:
        pass

    # 3.5 USB 独占（实验性）：上次开着且这次插着 -> 自动申请授权；没插就不打扰
    _usb_autostart()

    set_prompt(None)
    print(get_str("hint"))
    start_input_worker()

    # 4. 正式启动核心引擎
    run_playback(files, env, start_index=resume_index, start_offset=resume_offset, folder_paths=folder_paths)