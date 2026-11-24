import os
from core.registry import command
from strings import get_str

@command('ls', 'ls [N] / ls -all : 查看播放列表 (ls -all 查看全部)', aliases=('list', 'll'))
def cmd_ls(ctx, args):
    files = ctx.files
    if not files:
        print(get_str("empty_playlist"))
        return None

    total = len(files)
    cur = ctx.index

    # =============== 新增：处理 -all 参数 ===============
    if args and args[0] in ('-all', '--all', '-a'):
        lo = 0
        hi = total
    else:
        # 原来的逻辑：默认展示当前附近几首
        if args:
            try:
                radius = max(0, int(args[0]))
            except ValueError:
                radius = 5
        else:
            radius = 5 if total > 40 else total

        lo = max(0, cur - radius)
        hi = min(total, cur + radius + 1)

        # ===================================================

    print()
    print("=" * 55)
    print(get_str("playlist_title") + f"  [{cur + 1}/{total}]")
    print("=" * 55)
    if lo > 0:
        print(get_str("playlist_prev", count=lo))
    for i in range(lo, hi):
        marker = "♬" if i == cur else "  "
        name = os.path.basename(files[i])
        print(f" {marker} [{i + 1:>3}] {name}")
    if hi < total:
        print(get_str("playlist_next", count=total - hi))
    print("=" * 55)
    print()
    return None

# ================= 新增：自定义歌单 =================
import re as _re
from utils import playlists as _pl


def _parse_indexes(args):
    """解析 -n1,3-5 形式的序号表达式，返回 1-based 列表；无参数返回 None。"""
    if not args:
        return None
    token = args[0]
    if token.startswith('-n'):
        token = token[2:] or (args[1] if len(args) > 1 else '')
    token = token.replace('，', ',').replace(' ', '')
    if not token:
        return None
    result = []
    for part in token.split(','):
        if not part:
            continue
        m = _re.fullmatch(r'(\d+)-(\d+)', part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            result.extend(range(a, b + 1))
        elif part.isdigit():
            result.append(int(part))
    return result or None


def _count_arg(args, default=1):
    """读取 up2 / down 3 这类步长参数。"""
    if not args:
        return default
    try:
        return max(1, int(args[0]))
    except (TypeError, ValueError):
        return default


def _show_tracks(name, show_all=False, current_file=None):
    tracks = _pl.get_tracks(name)
    total = len(tracks)
    if total == 0:
        print(get_str("pl_no_tracks", name=name))
        return
    lo, hi = 0, total
    if not show_all and total > 40:
        lo, hi = 0, 25
    print()
    print("=" * 55)
    print(get_str("pl_tracks_title", name=name, total=total))
    print("=" * 55)
    for i in range(lo, hi):
        marker = "♬" if current_file and tracks[i] == current_file else "  "
        print(f" {marker} [{i + 1:>3}] {os.path.basename(tracks[i])}")
    if hi < total:
        print(get_str("pl_more", count=total - hi))
    print("=" * 55)
    print()


@command('pl', 'pl [歌单名]     : 歌单：pl 名字 切换 / pl help 看用法', aliases=('pls',), group='pl')
def cmd_playlist(ctx, args):
    if not args:
        data = _pl.load_all()
        current = getattr(ctx, 'playlist_name', '默认') or '默认'
        default_files = getattr(ctx, 'default_files', None) or ctx.files or []
        print()
        print("=" * 55)
        print(get_str("pl_list_title"))
        print("=" * 55)
        mark = "♬" if current == '默认' else " "
        print(f" {mark} 默认 (内置)  ({len(default_files)} 首)")
        for name in _pl.names():
            mark = "♬" if current == name else " "
            print(f" {mark} {name}  ({len(data[name].get('tracks', []))} 首)")
        print("=" * 55)
        print(get_str("pl_help_hint"))
        print()
        return None

    sub = args[0].lower()
    rest = args[1:]

    known = {'help', 'h', '?', 'show', 'new', 'create', 'del', 'delete',
             'rmlist', 'rename', 'add', 'a', 'rm', 'remove', 'play', 'load',
             'mv', 'move'}
    if sub not in known:
        # pl <歌单名> 直接切换
        if sub in ('默认', 'default'):
            print(get_str("pl_switch_default"))
            return ('play_default', None)
        real = _pl.find_existing(args[0])
        if real and len(args) == 1:
            print(get_str("pl_switch", name=real))
            return ('play_playlist', real)
        hint = _pl.similar(args[0])
        if hint:
            print(get_str("pl_not_found_hint", name=args[0], hint=hint))
        else:
            print(get_str("pl_not_found", name=args[0]))
        return None

    if sub in ('help', 'h', '?'):
        print("""
=================== 自定义歌单 ===================
  pls                      列出所有歌单
  pl show <名字> [all]     查看歌单内容
  pl new <名字>            新建歌单
  pl del <名字>            删除歌单
  pl rename <旧> <新>      重命名歌单
  pl add <名字> [-n1,3-5]  添加曲目（不加 -n 则添加当前播放这首）
  pl rm <名字> -n1,3       从歌单移除曲目（只是移出歌单，不删文件）
  pl mv <名字> -n5 1       把歌单第 5 首移到第 1 位（负数从末尾数，-1=最后一首）
  pl mv <名字> -n5 up|down 上移 / 下移，默认 1 位，可跟次数（up3）
  pl mv <名字> -n5 top|bottom  直接置顶 / 放到末尾
  pl <名字>                切换到该歌单（等同 pl play <名字>）
  pl 默认                  切回内置的默认歌单
  pl play <名字>           用这个歌单作为当前播放列表
==================================================
""")
        return None

    if sub == 'show':
        if not rest:
            print(get_str("pl_need_name"))
            return None
        name = rest[0]
        if not _pl.exists(name):
            print(get_str("pl_not_found", name=name))
            return None
        _show_tracks(name, show_all=(len(rest) > 1 and rest[1] in ('all', '-all', '-a')),
                     current_file=ctx.file_path)
        return None

    if sub in ('new', 'create'):
        if not rest:
            print(get_str("pl_need_name"))
            return None
        name = rest[0]
        if _pl.clean_name(name).casefold() in {r.casefold() for r in _pl.RESERVED_NAMES}:
            print(get_str("pl_reserved", name=name))
            return None
        if _pl.exists(name):
            print(get_str("pl_exists", name=name))
            return None
        if _pl.create(name):
            print(get_str("pl_created", name=name))
        else:
            print(get_str("pl_create_fail", name=name))
        return None

    if sub in ('del', 'delete', 'rmlist'):
        if not rest:
            print(get_str("pl_need_name"))
            return None
        name = rest[0]
        if _pl.delete(name):
            print(get_str("pl_deleted", name=name))
        else:
            print(get_str("pl_not_found", name=name))
        return None

    if sub == 'rename':
        if len(rest) < 2:
            print(get_str("pl_rename_usage"))
            return None
        if _pl.rename(rest[0], rest[1]):
            print(get_str("pl_renamed", old=rest[0], new=rest[1]))
        else:
            print(get_str("pl_rename_fail", old=rest[0], new=rest[1]))
        return None

    if sub in ('add', 'a'):
        if not rest:
            print(get_str("pl_add_usage"))
            return None
        name = rest[0]
        if not _pl.exists(name):
            print(get_str("pl_not_found", name=name))
            return None
        indexes = _parse_indexes(rest[1:])
        if indexes is None:
            if not ctx.file_path:
                print(get_str("pl_nothing_playing"))
                return None
            paths = [ctx.file_path]
        else:
            paths = []
            for i in indexes:
                if 1 <= i <= len(ctx.files):
                    paths.append(ctx.files[i - 1])
            if not paths:
                print(get_str("pl_bad_index"))
                return None
        added = _pl.add_tracks(name, paths)
        print(get_str("pl_added", count=added, name=name))
        return None

    if sub in ('rm', 'remove'):
        if len(rest) < 2:
            print(get_str("pl_rm_usage"))
            return None
        name = rest[0]
        indexes = _parse_indexes(rest[1:])
        if not indexes:
            print(get_str("pl_rm_usage"))
            return None
        count, removed = _pl.remove_tracks(name, indexes)
        if count == 0:
            print(get_str("pl_rm_none"))
        else:
            print(get_str("pl_removed", count=count, name=name))
            for p in removed:
                print("   - " + os.path.basename(p))
        return None

    if sub in ('mv', 'move'):
        if len(rest) < 3:
            print(get_str("pl_mv_usage"))
            return None
        name = rest[0]
        real = _pl.find_existing(name)
        if not real:
            hint = _pl.similar(name)
            if hint:
                print(get_str("pl_not_found_hint", name=name, hint=hint))
            else:
                print(get_str("pl_not_found", name=name))
            return None

        pos_args = rest[1:]
        indexes = _parse_indexes(pos_args)
        if not indexes:
            print(get_str("pl_mv_usage"))
            return None
        if len(indexes) != 1:
            print(get_str("pl_mv_one_only"))
            return None
        src = indexes[0]

        # 跳过序号本身，取出目标参数
        consumed = 1
        if pos_args[0].startswith('-n') and len(pos_args[0]) == 2:
            consumed = 2
        tail = pos_args[consumed:]
        if not tail:
            print(get_str("pl_mv_usage"))
            return None

        word = tail[0].lower()
        target = delta = None
        m = _re.fullmatch(r'(up|u|down|d|上移|下移|上|下)(\d+)?', word)
        if m:
            step = int(m.group(2)) if m.group(2) else _count_arg(tail[1:])
            step = max(1, step)
            delta = -step if m.group(1) in ('up', 'u', '上移', '上') else step
        elif word in ('top', 'first', '顶', '置顶', '首'):
            target = 1
        elif word in ('bottom', 'last', '底', '末尾', '尾'):
            target = -1
        else:
            try:
                target = int(word)
            except ValueError:
                print(get_str("pl_mv_bad_target", arg=tail[0]))
                return None

        new_pos, item = _pl.move_track(real, src, target=target, delta=delta)
        if new_pos is None:
            print(get_str("pl_mv_fail", name=real, index=src))
            return None
        if new_pos == src:
            print(get_str("pl_mv_same", index=src))
            return None
        print(get_str("pl_mv_done", name=real, old=src, new=new_pos,
                      file=os.path.basename(item)))
        return ('pl_reordered', real)

    if sub in ('play', 'load'):
        if not rest:
            print(get_str("pl_need_name"))
            return None
        real = _pl.find_existing(rest[0])
        if not real:
            hint = _pl.similar(rest[0])
            if hint:
                print(get_str("pl_not_found_hint", name=rest[0], hint=hint))
            else:
                print(get_str("pl_not_found", name=rest[0]))
            return None
        if not _pl.get_tracks(real):
            print(get_str("pl_empty", name=real))
            return None
        return ('play_playlist', real)

    print(get_str("pl_unknown_sub", sub=sub))
    return None
