import os
import re as _re
from core.registry import command
from utils.formatter import format_time
from strings import get_str

# 新增：解析 -n 参数（支持 -n3 或 -n 3 两种写法）
def _parse_n_arg(args):
    if not args:
        return None
    token = args[0]
    if token.startswith('-n'):
        num_str = token[2:]
        if not num_str and len(args) > 1:
            num_str = args[1]
        if not _re.fullmatch(r'[0-9]+', num_str or ''):
            return None
        try:
            return int(num_str)
        except ValueError:
            return None
    return None

@command('next', 'next / n        : 切换到下一首 (next -nN 跳 N 首)', aliases=('n',))
def cmd_next(ctx, args):
    step = 1
    n_val = _parse_n_arg(args)
    if n_val is not None:
        step = max(1, n_val)
    return ('next', step)

@command('last', 'last / l        : 切换到上一首 (last -nN 退 N 首)', aliases=('l', 'prev', 'p'))
def cmd_last(ctx, args):
    step = 1
    n_val = _parse_n_arg(args)
    if n_val is not None:
        step = max(1, n_val)
    return ('last', step)

@command('replay', 'replay / r      : 重新播放当前这首', aliases=('r',))
def cmd_replay(ctx, args):
    return 'replay'

@command('', '(直接按回车)     : 暂停 / 继续')
def cmd_empty(ctx, args):
    player = ctx.player
    if player is None or not player.is_playing():
        print(get_str("pause_none"))
        return None

    if player.is_paused():
        if player.resume():
            ctx.paused = False
            ctx.offset = player.current_position()
            print(get_str("resumed", pos=format_time(ctx.offset)))
        else:
            print(get_str("resume_fail"))
    else:
        if player.pause():
            ctx.paused = True
            ctx.offset = player.current_position()
            print(get_str("paused", pos=format_time(ctx.offset)))
        else:
            print(get_str("pause_fail"))
    return None

def _parse_seconds(args):
    if not args: return None
    token = args[0].strip()
    # 明确拒绝 -n 格式，防止冲突
    if token.startswith('-n'):
        return None
    while token and token[0] in '><':
        token = token[1:]
    if not _re.fullmatch(r'[+-]?[0-9]+(\.[0-9]+)?', token or ''):
        return None
    try:
        return float(token)
    except ValueError:
        return None

@command('forward', 'forward 30 / +30 : 进度 ±N 秒（+30 / -15）', aliases=('ff', 'seek'))
def cmd_forward(ctx, args):
    seconds = _parse_seconds(args)
    if seconds is None:
        print(get_str("usage_forward"))
        return None
    return ('seek', seconds)

@command('switch', 'switch -nN / s -nN : 切换到第 N 首 (如: switch -n3)', aliases=('s',))
def cmd_switch(ctx, args):
    if not args:
        print(get_str("switch_invalid", total=ctx.total))
        return None

    token = args[0]
    # 强制要求 -n 前缀，与 next/last 风格统一
    if not token.startswith('-n'):
        print(get_str("switch_need_n"))
        return None

    num_str = token[2:] or (args[1] if len(args) > 1 else '')
    try:
        target_num = int(num_str)
        if 1 <= target_num <= ctx.total:
            print(get_str("switch_success", num=target_num))
            return ('switch', target_num - 1)  # 转换为从0开始的索引
        else:
            print(get_str("switch_invalid", total=ctx.total))
    except ValueError:
        print(get_str("switch_invalid", total=ctx.total))

    return None

@command('loop', 'loop (lp)       : 切换单曲循环', aliases=('lp',), group='play')
def cmd_loop(ctx, args):
    ctx.loop_mode = not ctx.loop_mode
    if ctx.loop_mode:
        ctx.list_loop_mode = False   # 单曲循环与列表循环互斥
    print(get_str("loop_on") if ctx.loop_mode else get_str("loop_off"))
    return None

@command('listloop', 'listloop (lc)   : 切换列表循环（播完最后一首回到第一首）', aliases=('lc', 'cycle'), group='play')
def cmd_list_loop(ctx, args):
    ctx.list_loop_mode = not ctx.list_loop_mode
    if ctx.list_loop_mode:
        ctx.loop_mode = False
    print(get_str("list_loop_on") if ctx.list_loop_mode else get_str("list_loop_off"))
    return None

@command('info', 'info / i        : 显示当前歌曲详细信息（标签/编码/大小/封面）', aliases=('i',))
def cmd_info(ctx, args):
    from core.scanner import print_detail_info
    if not ctx.meta:
        print(get_str("no_current_song"))
        return None
    print_detail_info(ctx.meta, ctx.index + 1, ctx.total)
    return None

@command('status', 'status / st     : 查看当前播放状态', aliases=('st',))
def cmd_status(ctx, args):
    from core.engine import MAX_FAIL_STREAK
    loop = get_str("on") if ctx.loop_mode else get_str("off")
    listloop = get_str("on") if ctx.list_loop_mode else get_str("off")
    rnd = get_str("on") if ctx.random_mode else get_str("off")
    print()
    print("=" * 55)
    print(get_str("status_title"))
    print("=" * 55)
    print(get_str("status_playlist", name=getattr(ctx, 'playlist_name', '默认')))
    if ctx.total:
        name = os.path.basename(ctx.file_path) if ctx.file_path else "-"
        print(get_str("status_track", index=ctx.index + 1, total=ctx.total, file=name))
    print(get_str("status_modes", loop=loop, listloop=listloop, random=rnd))
    try:
        from utils import output as _out
        print(f"  输出   : {_out.summary_line()}")
    except Exception:
        try:
            from utils.usbdac import summary_line as _out_line
            print(f"  输出   : {_out_line()}")
        except Exception:
            pass
    if ctx.fail_streak:
        print(get_str("status_fail", count=ctx.fail_streak, max=MAX_FAIL_STREAK))
    print("=" * 55)
    print()
    return None

@command('random', 'random (rnd)    : 切换随机播放', aliases=('rnd',), group='play')
def cmd_random(ctx, args):
    ctx.random_mode = not ctx.random_mode
    print(get_str("random_on") if ctx.random_mode else get_str("random_off"))
    return None
