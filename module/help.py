from core.registry import (command, NAMESPACES, resolve_namespace,
                           namespace_name, group_commands, basic_commands)
from strings import get_str

PL_HELP = """
  歌单常用操作（pl 也能直接跟歌单名切换）:
    pl                   列出所有歌单（含内置默认歌单）
    pl <名字>            切换到该歌单
    pl 默认              切回内置默认歌单
    pl show <名字> [all] 查看歌单曲目
    pl new <名字>        新建歌单（重名 / 保留名会被拒绝）
    pl del <名字>        删除歌单
    pl rename <旧> <新>  重命名歌单
    pl add <名字> [-n1,3-5]
                         添加曲目（不加 -n 则添加当前播放这首）
    pl rm <名字> -n1,3   从歌单移除曲目（不删文件）
    pl play <名字>       切换到该歌单
"""


def _print_group(title, items, extra=''):
    print("=" * 55)
    print(title)
    print("=" * 55)
    seen = set()
    for _name, text in items:
        if text in seen:
            continue
        seen.add(text)
        print("  " + text)
    if extra:
        print(extra.rstrip())
    print("=" * 55)


def _print_all():
    print()
    _print_group(get_str("help_basic_title"), basic_commands())
    for ns in NAMESPACES:
        print()
        _print_group(get_str("help_group_title", name=f"{namespace_name(ns)} ({ns})"),
                     group_commands(ns),
                     PL_HELP if ns == 'pl' else '')
        print(get_str("help_group_hint", ns=ns))
    print()


@command('help', 'help (h/?)      : 基础帮助 / help <分类> / help all', aliases=('h', '?', '帮助'))
def cmd_help(ctx, args):
    arg = args[0].lower() if args else ''

    if arg in ('all', '-all', '全部'):
        _print_all()
        return None

    ns = resolve_namespace(arg) if arg else None
    if ns:
        print()
        _print_group(get_str("help_group_title", name=f"{namespace_name(ns)} ({ns})"),
                     group_commands(ns),
                     PL_HELP if ns == 'pl' else '')
        print(get_str("help_group_hint", ns=ns))
        print()
        return None

    # 默认：只显示表层基础命令 + 分类入口
    print()
    _print_group(get_str("help_basic_title"), basic_commands())
    print()
    print("=" * 55)
    print(get_str("help_ns_title"))
    print("=" * 55)
    for ns, (cn, aliases) in NAMESPACES.items():
        if ns == 'pl':
            count = 9
        else:
            count = len(group_commands(ns))
        print(f"  {cn} ({ns}) : 输入 {ns} 进入 / {ns}-命令 直接用（{count} 项）")
    print("=" * 55)
    print(get_str("help_hint_footer"))
    print()
    return None
