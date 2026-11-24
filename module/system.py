from core.registry import command

@command('quit', 'quit / q / exit : 退出播放器', aliases=('q', 'exit'))
def cmd_quit(ctx, args):
    return 'quit'

# ================= 新增：重新扫描媒体库 =================
import os

_SCAN_FLAG_RECURSIVE = ('-r', '--recursive', '递归')
_SCAN_FLAG_FLAT = ('-n', '--no-recursive', '--flat', '非递归')
_SCAN_FLAG_INCR = ('-i', '--incr', '--incremental', '增量')
_SCAN_FLAG_FULL = ('-a', '--full', '全量')


def _parse_scan_args(args):
    """解析扫描参数：路径 + 递归开关 + 增量开关。"""
    paths = []
    recursive = None      # None = 由调用方决定（给路径默认不递归）
    incremental = False
    for raw in args or []:
        low = raw.lower()
        if low in _SCAN_FLAG_RECURSIVE:
            recursive = True
        elif low in _SCAN_FLAG_FLAT:
            recursive = False
        elif low in _SCAN_FLAG_INCR:
            incremental = True
        elif low in _SCAN_FLAG_FULL:
            incremental = False
        else:
            paths.append(os.path.abspath(os.path.expanduser(raw)))
    return paths, recursive, incremental


@command('rescan',
         'rescan (rs)     : 完整重扫媒体库，重建缓存\n'
         '                  rescan [路径...] [-r]   只重扫指定路径 (-r 递归子目录)\n'
         '                  rescan -i [路径...] [-r] 改为增量扫描',
         aliases=('rs',), group='sys')
def cmd_rescan(ctx, args):
    paths, recursive, incremental = _parse_scan_args(args)
    action = 'refresh' if incremental else 'rescan'
    return (action, {'paths': paths, 'recursive': recursive,
                     'incremental': incremental})


# ================= 新增：增量扫描（不重置缓存） =================
@command('refresh',
         'refresh (rf)    : 增量扫描，只增删差异，不动缓存、不打断播放\n'
         '                  refresh [路径...] [-r]  指定路径 (-r 递归子目录)',
         aliases=('rf', 'incr', '增量扫描'), group='sys')
def cmd_refresh(ctx, args):
    paths, recursive, _ = _parse_scan_args(args)
    return ('refresh', {'paths': paths, 'recursive': recursive,
                        'incremental': True})




@command('compat', 'compat          : 跨平台兼容自检（Windows / 桌面Linux / Termux）',
         aliases=('兼容',), group='sys')
def cmd_compat(ctx, args):
    from utils.compat import report
    report()
    return None

# ================= 新增：多设备选择（白名单 / 指定设备） =================
def _device_rows():
    """列出 (路径, 缓存身份, 是否当前选中)。"""
    from utils import usbdac
    devs = usbdac.list_devices()
    d = usbdac.load_profiles()
    st = usbdac._settings()
    cur_path = st.get('usb_device')
    cur_id = st.get('usb_vidpid')
    rows = []
    for path in devs:
        key = (d.get('paths') or {}).get(path)
        prof = (d.get('profiles') or {}).get(key) if key else None
        name = ''
        if prof:
            name = '%s:%s %s' % (prof.get('vid'), prof.get('pid'),
                                 prof.get('product') or '')
        sel = (path == cur_path) or (key and key == cur_id)
        rows.append({'path': path, 'key': key or '', 'name': name, 'selected': bool(sel)})
    return rows


@command('usblist', 'usblist (usbdev) : 列出所有 USB 设备与已缓存的身份', aliases=('usbdev',), group='set')
def cmd_usb_list(ctx, args):
    from utils import usbdac
    from utils import exclusive
    backend = exclusive.detect_backend()
    if backend != 'termux':
        devs = exclusive.list_mpv_devices()
        cfg = exclusive.get_config() or {}
        print()
        print("=" * 55)
        print(f"🔌 音频设备列表（后端：{exclusive.backend_note()}）")
        print("=" * 55)
        if not devs:
            print("  没有枚举到独立设备（mpv 不在或驱动没暴露直通设备）")
        for i, (did, desc) in enumerate(devs, 1):
            mark = ' ←当前' if did == cfg.get('device') else ''
            print(f"  {i}. {did}{mark}")
            print(f"     {desc}")
        print()
        print("  切换设备：usbuse <编号 | 设备ID>")
        print("=" * 55)
        print()
        return None

    rows = _device_rows()
    print()
    print("=" * 55)
    print("🔌 USB 设备列表")
    print("=" * 55)
    if not rows:
        print("  没有检测到 USB 设备（小尾巴插好了吗？）")
        print("=" * 55)
        print()
        return None
    for i, r in enumerate(rows, 1):
        mark = ' ←当前' if r['selected'] else ''
        print(f"  {i}. {r['path']}{mark}")
        if r['name']:
            print(f"     身份: {r['name']}")
        else:
            print("     身份: 未探测（用 usbuse {} 授权后即可识别）".format(i))
    print()
    print("  切换设备：usbuse <编号 | VID:PID | 路径>")
    print("=" * 55)
    print()
    return None


@command('usbuse', 'usbuse <编号|VID:PID|路径> : 指定用哪个 USB 设备（白名单）', group='set')
def cmd_usb_use(ctx, args):
    from utils import usbdac
    if not args:
        print("用法：usbuse 1  /  usbuse 06cb:1594  /  usbuse /dev/bus/usb/001/002")
        return None

    want = args[0].strip()
    from utils import exclusive
    if exclusive.detect_backend() != 'termux':
        devs = exclusive.list_mpv_devices()
        chosen = None
        if want.isdigit() and 1 <= int(want) <= len(devs):
            chosen = devs[int(want) - 1][0]
        else:
            for did, desc in devs:
                if want == did or want.lower() in (did + ' ' + desc).lower():
                    chosen = did
                    break
        if not chosen:
            print(f"✘ 没找到设备：{want}（先 usblist）")
            return None
        exclusive.save_config(True, device=chosen)
        print(f"✔ 已指定设备：{chosen}")
        print("  下次播放生效。")
        return None

    rows = _device_rows()

    chosen = None
    # 按路径 / VID:PID 匹配
    for r in rows:
        if want == r['path'] or (r['key'] and want.lower() == r['key'].lower()):
            chosen = r
            break
    # 按编号匹配
    if chosen is None and want.isdigit():
        idx = int(want)
        if 1 <= idx <= len(rows):
            chosen = rows[idx - 1]

    if chosen is None:
        print(f"✘ 没找到设备：{want}")
        print("  先 usblist 看看有哪些")
        return None

    usbdac._patch_settings({'usb_device': chosen['path'],
                            'usb_vidpid': chosen['key'] or ''})
    print(f"✔ 已指定设备：{chosen['path']}")
    if chosen['name']:
        print(f"  身份：{chosen['name']}")
    else:
        print("  还没探测过它的拓扑，正在申请授权并探测…")
        ok, text = usbdac.request_permission(chosen['path'])
        for line in (text or '').splitlines():
            print("  " + line)
        if not ok:
            print("  ✘ 探测失败：%s" % text)
            return None
    print("  下次播放生效；usb on 会确保它是独占目标。")
    return None

# ================= 新增：输出通道（AudioTrack / 系统默认） =================
@command('audio', 'audio [on|off]  : 输出通道：AudioTrack 直通(默认) / 系统默认',
         aliases=('输出', 'audiotrack'), group='set')
def cmd_audio(ctx, args):
    from utils import output

    sub = (args[0].strip().lower() if args else '')
    if sub in ('on', 'aaudio', 'audiotrack', '开'):
        output.set_mode(output.MODE_AUDIOTRACK)
    elif sub in ('off', 'auto', '系统', '关'):
        output.set_mode(output.MODE_AUTO)

    ok, why = output.aaudio_available()
    print()
    print("=" * 55)
    print("🔈 输出通道")
    print("=" * 55)
    print(f"  当前 : {output.mode_note()}")
    if ok:
        print("  说明 : --ao=aaudio + low-latency，走 Android 的 AudioTrack/AAudio，")
        print("         不经我们自己的重采样/上混；极致完美输出打开时再加")
        print("         --audio-format=float 与 --audio-samplerate=<源采样率>。")
    else:
        print(f"  ⚠️ {why}")
    print("  切换 : set-audio on（AudioTrack） / set-audio off（系统默认）")
    print("=" * 55)
    print()
    return None


# ================= 新增：极致完美输出 / 音量（与 usb 同级，group=set） =================
@command('perfect', 'perfect (pure)  : 极致完美输出：源格式直送，不重采样不做任何处理（再输一次关闭）',
         aliases=('pure', 'hifi', '极致'), group='set')
def cmd_perfect(ctx, args):
    from utils import usbdac

    cur = usbdac.perfect_enabled()
    sub = (args[0].lower() if args else '')
    if sub in ('on', '开', 'true', '1'):
        new = True
    elif sub in ('off', '关', 'false', '0'):
        new = False
    else:
        new = not cur
    usbdac.set_perfect(new)

    ok_plat, why_plat = usbdac.usb_supported()
    print()
    print("=" * 55)
    if new and not ok_plat:
        print("💎 极致完美输出：已记录为开启，但当前平台用不上")
        print(f"   原因：{why_plat}")
        print("   它只在 Termux + usb on（USB 独占）时真正起作用")
        print("=" * 55)
        print()
        return None
    if new:
        print("💎 极致完美输出：已开启（已存入 progress.json，下次启动仍然有效）")
        print("-" * 55)
        print("  采样率   : 强制用音频文件自己的采样率，绝不重采样")
        print("  比特率   : 跟着源文件走，不压缩、不重编码")
        print("  精度     : 内部按 float32 计算，送出 32 bit，中间不截断")
        print("  处理     : 不加均衡、不做动态、不做抖动、不混音")
        print("  生效范围 : AudioTrack/AAudio（默认）与 USB 独占（实验性）都生效")
        try:
            from utils import output as _out
            if _out.is_audiotrack():
                print("  平台参数 : --ao=aaudio --aaudio-performance-mode=low-latency")
                print("             --audio-format=float（AAudio 原生 float32，不做 s16 往返）")
                print("             --audio-samplerate=<源采样率>（能否真按源开流看 HAL）")
                print("             播放器自己不做重采样/上混，其余交给 Android")
        except Exception:
            pass
        prof = usbdac.get_profile()
        caps = []
        for bits in (16, 24, 32):
            a = usbdac._alt_of(prof, bits) if prof else None
            if not a:
                continue
            mps = a.get('mps') or 0
            top = (mps * 1000) // (2 * (bits // 8)) if mps else 0
            if top:
                caps.append('%dbit 最高 %s' % (bits, ('%.1f kHz' % (top / 1000.0)).rstrip('0').rstrip('.')))
        if caps:
            print("  实际能力 : " + ' / '.join(caps))
            print("             44.1kHz、48kHz 都是原生直送（44.1k 按 44/45 帧交替分包，不重采样）")
            print("             更高的 88.2k/96k 超出端点单包容量，会明确提示并降级，绝不偷偷重采样")
        else:
            print("  注意     : 44.1kHz/48kHz 原生直送；更高的采样率受端点容量限制")
    else:
        print("💎 极致完美输出：已关闭（回到普通独占模式：位深跟源走，超范围回落 48k）")
    print("=" * 55)
    print()
    return None


@command('vol', 'vol [0-100|+|/|hw|sw] : 音量（硬件音量 50 刻度 / 软件音量）',
         aliases=('volume', 'v'), group='set')
def cmd_vol(ctx, args):
    from utils import usbdac

    mode = usbdac.get_volume_mode()
    step = usbdac.get_volume_step()
    sub = (args[0].strip().lower() if args else '')

    # ---- 切换音量控制方式 ----
    if sub in ('hw', '硬件', 'hardware'):
        from utils import exclusive
        usbdac.set_volume_mode('hw')
        ok_hw, why_hw = exclusive.hw_volume_supported()
        if not ok_hw:
            print(f"⚠️ 已记录为硬件音量，但{why_hw}")
            print("   （设置已保存；vol sw 可切回软件音量）")
            return None
        if exclusive.detect_backend() == 'wasapi':
            print("✔ 音量改为【硬件音量】：Windows 端点音量（pycaw，直接调系统主音量）")
            print("   ⚠️ WASAPI 独占会绕过 Windows 音频引擎，部分声卡此时端点音量可能不起作用；")
            print("      如果调了没反应，用 vol sw 改走软件音量。")
            return None
        print("✔ 音量改为【硬件音量】：直接调小尾巴的 UAC2 Feature Unit，")
        print("   一共 50 个刻度（每格 2%），不经过任何软件衰减，也不损失精度。")
        if ctx.player is not None and getattr(ctx.player, 'usb', None) is not None:
            pct = usbdac.volume_percent()
            if ctx.player.set_volume(pct):
                print(f"   已实时生效：{pct}%")
            else:
                print("   ⚠️ 这个设备没有硬件音量（或不支持热改），建议改用 vol sw")
        print("   注意：硬件音量会被系统音量键共享，系统音量调到 0 时仍然没声。")
        return None

    if sub in ('sw', '软件', 'software'):
        usbdac.set_volume_mode('sw')
        print("✔ 音量改为【软件音量】：在 ffmpeg 里用 float 做增益，")
        print("   优点是一定能用、能精确到小数级；缺点是极致完美输出下也算一次处理。")
        print("   改动在当前这首播完后的下一首生效。")
        return None

    # ---- 调音量 ----
    if sub in ('+', '++', 'up'):
        step = usbdac.set_volume_step(step + 1)
        changed = True
    elif sub in ('-', '--', 'down'):
        step = usbdac.set_volume_step(step - 1)
        changed = True
    elif sub:
        try:
            want = float(sub)
        except ValueError:
            print("用法：vol 60 / vol + / vol - / vol hw / vol sw")
            return None
        want = max(0.0, min(100.0, want))
        step = usbdac.set_volume_step(int(round(want / 2.0)))
        changed = True
    else:
        changed = False

    pct = usbdac.volume_percent(step)
    applied = None
    if changed and mode == 'hw' and ctx.player is not None and getattr(ctx.player, 'usb', None) is not None:
        applied = ctx.player.set_volume(pct)

    print()
    print("=" * 55)
    print(f"🔊 音量：{pct}%   （硬件刻度 {step}/50，每格 2%）")
    print(f"   控制方式：{'硬件音量（UAC2，零点衰减）' if mode == 'hw' else '软件音量（ffmpeg float 增益）'}")
    if changed:
        if mode == 'hw':
            if applied:
                print("   已实时生效 ✔")
            elif ctx.player is not None and getattr(ctx.player, 'usb', None) is not None:
                print("   ⚠️ 实时设置失败（设备可能不支持热改音量）")
            else:
                print("   下次播放生效（当前没在走 USB 独占）")
        else:
            print("   下次播放生效（软件音量无法热改当前流）")
    print("=" * 55)
    print()
    return None

# ================= 新增：USB 音频检测 / 独占输出（可选） =================
import sys


@command('usb', 'usb [on|off|test|probe|cleanup|panic] : 检测 / 开关 USB 独占输出（无 root）', group='set')
def cmd_usb(ctx, args):
    from utils import usbdac

    sub = args[0].lower() if args else ''

    # ---------- 关闭 ----------
    if sub == 'off':
        from utils import exclusive
        cfg = usbdac.load_config()
        usbdac.save_config({'enabled': False, 'device': cfg.get('device') or ''})
        exclusive.save_config(False)
        usbdac.set_force(False)
        print("✔ 已关闭独占输出（已存入 progress.json，下次启动不会再启用）")
        # 正在独占播放时，先把接口还回去，否则系统音频会是哑的
        if ctx.player is not None and getattr(ctx.player, 'usb', None) is not None:
            try:
                ctx.player.stop()
                print("  已停止当前独占播放，并把 USB 接口交还内核驱动")
                print("  （snd-usb-audio 重新接管，AudioFlinger 可以正常出声了）")
            except Exception as exc:
                print("  停止播放时出错：%s" % exc)
        else:
            print("  当前没有独占播放，系统本来就持有 USB 控制权")
        return None

    # ---------- 打开：弹窗申请 + 记住设备 ----------
    extra = args[1].strip().lower() if len(args) > 1 else ''
    # 默认就走 force：Android 一直占着 snd-usb-audio，不抢占 = 等于没开
    force = extra != 'safe'
    if sub == 'on' and extra and extra not in ('force', '强制', 'safe', '安全'):
        print("用法：set-usb on            （强制独占，默认）")
        print("      set-usb on safe       （不抢占内核驱动，只在设备空闲时独占）")
        return None

    if sub == 'on':
        from utils import exclusive
        backend = exclusive.detect_backend()
        if backend != 'termux':
            ok, why = exclusive.available()
            if not ok:
                print("⚠️ " + why)
                return None
            devs = exclusive.list_mpv_devices()
            dev = exclusive.guess_device(devs)
            exclusive.save_config(True, device=dev or '')
            print()
            print(f"✔ 已开启独占输出（后端：{exclusive.backend_note()}）")
            if devs:
                for i, (did, desc) in enumerate(devs, 1):
                    mark = ' ←选中' if did == dev else ''
                    print(f"   {i}. {did}  ({desc}){mark}")
            else:
                print("   没枚举到独立设备，先用默认设备；可用 usblist / usbuse 指定")
            print("   下次播放生效（mpv 会直接抢硬件，绕过系统混音器）")
            print("   usb off 可回退。")
            return None

        ok, why = usbdac.have_tools()
        if not ok:
            print("⚠️ " + why)
            return None
        devs = usbdac.list_devices()
        if not devs:
            print("⚠️ 没有检测到 USB 设备 —— 小尾巴插好了吗？")
            return None
        target = usbdac.pick_device() or devs[0]
        print(f"  检测到 {len(devs)} 个可访问设备: {', '.join(devs)}")
        if len(devs) > 1:
            print(f"  按白名单优先选择: {target}（要换用 usbuse）")
        print("  正在申请访问权限…")
        print("  ⚠️ 屏幕上会弹出「允许 termux 访问 CX31993+MAX97220 吗」")
        print("     请点【确定】（这是一次性授权，之后不必再点）")
        # 抢占内核驱动是有代价的：默认不做，要用 usb on force
        usbdac.set_force(force)
        if force:
            print("  ⚠️ 强制独占【实验性】：从 snd-usb-audio 手里接管整块设备。")
            print("     · 播放期间系统/Poweramp 会没声（正常，说明真独占了）")
            print("     · 这台内核不支持 USBDEVFS_CONNECT，播完驱动通常绑不回来，")
            print("       需要拔插一次小尾巴才能恢复系统音频")
            print("     · 已知不稳：反复抢占可能让接口进入 busy 状态，此时也必须拔插")
        else:
            print("  ⚠️ safe 模式不抢占内核驱动：接口被系统占着时会安全退出。")
            print("     Android 几乎总是占着 USB 声卡，所以 safe 模式通常等于不独占。")
        ok, text = usbdac.request_permission(target)
        print()
        for line in (text or '').splitlines():
            print("  " + line)
        if not ok:
            print("✘ 开启失败：%s" % text)
            return None
        cfg = usbdac.load_config()
        cfg['enabled'] = True
        cfg['device'] = devs[0]
        usbdac.save_config(cfg)
        print()
        print(f"✔ 已开启 USB 独占输出: {target}")
        prof = usbdac.get_profile(target)
        if prof:
            print(f"   设备：{prof.get('vid')}:{prof.get('pid')} {prof.get('product') or ''}"
                  f"（{'UAC2' if prof.get('uac2') else 'UAC1'}，时钟源 {prof.get('clock_id')}，"
                  f"音量控制{'有' if prof.get('has_volume') else '无'}）")
        print("  下次播放生效：ffmpeg 解码裸 PCM → usbsink → USB 等时流（绕过混音器）")
        print("  正在播的这首不会自动换路，输入 replay 重开一次即走独占输出")
        print("  判断是否真独占：播放时打开 Poweramp/系统音乐应当没声")
        print("  播放失败可用 usb off 一键回退（会把 USB 交还系统）。")
        return None

    # ---------- 锁定输出采样率（设备时钟改不动时的折中） ----------
    if sub in ('rate', '采样率', '锁定'):
        cur = usbdac.get_rate_lock()
        if not args[1:]:
            if cur:
                print(f"  当前锁定: {cur} Hz（0 或 auto 取消）")
            else:
                print("  当前: 跟随源文件（未锁定）")
            print("  用法: set-usb rate 48000   或   set-usb rate auto")
            return None
        val = args[1].strip().lower()
        if val in ('auto', '0', '跟随'):
            usbdac.set_rate_lock(0)
            print("✔ 已取消采样率锁定，恢复跟随源文件")
        else:
            try:
                hz = int(float(val))
            except ValueError:
                print("✘ 采样率要写成数字，例如 set-usb rate 48000")
                return None
            if hz < 8000 or hz > 384000:
                print("✘ 采样率超出范围（8000 ~ 384000）")
                return None
            usbdac.set_rate_lock(hz)
            print(f"✔ 已锁定输出为 {hz} Hz")
            print("  适用场景：小尾巴的时钟改不动（内核 usbfs 拦住采样率请求），")
            print("  导致音高偏低/偏高时，用它把发包速率对齐设备实际时钟。")
            print("  代价：源文件采样率不同时会轻微变速。下次播放生效。")
        return None

    # ---------- 清理残留独占进程 ----------
    if sub in ('cleanup', 'clean', '清理'):
        import subprocess
        n = usbdac.sweep_sinks()
        left = subprocess.run(['pgrep', '-fa', 'audioplayer_usb/bin/usbsink'],
                              capture_output=True, text=True).stdout.strip()
        print("✔ 已发送 TERM 给残留的独占进程（它们会自己把 USB 接口还回去）" if n
              else "  没有发现残留的独占进程")
        if left:
            print("  仍在运行的：")
            for line in left.splitlines()[:5]:
                print("   " + line)
            print("  如果一直不走，用 set-usb panic 看恢复步骤")
        return None

    # ---------- 紧急恢复：系统音频没声了怎么办 ----------
    if sub in ('panic', 'restore', '恢复'):
        print()
        print("=" * 55)
        print("🚑 USB 独占紧急恢复")
        print("=" * 55)
        # 1) 先把播放器自己的独占停掉
        if ctx.player is not None and getattr(ctx.player, 'usb', None) is not None:
            try:
                ctx.player.stop()
                print("  1) 已停止独占播放，usbsink 会归还接口并重新绑定内核驱动")
            except Exception as exc:
                print("  1) 停止独占时出错：%s" % exc)
        else:
            print("  1) 当前没有独占播放在跑")
        usbdac.save_config({'enabled': False})
        from utils import exclusive
        exclusive.save_config(False)
        usbdac.set_force(False)
        print("  2) 已关闭独占设置（并清掉 force 标记）")
        print()
        print("  如果系统还是没声音，按顺序试：")
        print("    a. 把小尾巴拔下来，等 3 秒再插上（最有效，重新枚举设备）")
        print("    b. 开发者选项里把「停用 USB 音频转接」开关来回切一次")
        print("    c. 开关一次蓝牙，或插拔一次有线耳机（触发音频路由重算）")
        print("    d. 都不行只能重启手机（无 root 无法重启 audioserver）")
        print("  以后想独占又怕弄哑系统音频，就只在「看视频/听歌专用」时用 usb on force，")
        print("  平时保持 usb off。")
        print("=" * 55)
        print()
        return None

    # ---------- 放 3 秒正弦波自测（不经过 decoded 音频管线） ----------
    if sub in ('test', 'tone'):
        import subprocess
        from utils import exclusive
        if exclusive.detect_backend() != 'termux':
            mpv = exclusive.mpv_path()
            if not mpv:
                print("⚠️ 缺少 mpv，无法自测")
                return None
            args = [mpv, '--no-video', '--really-quiet', '--no-terminal',
                    'av://lavfi:sine=frequency=440:duration=3']
            args += exclusive.mpv_args(perfect=False)
            print("  即将播放 3 秒正弦波，请把音量调小一点")
            try:
                subprocess.run(args, timeout=30)
                print("✔ 自测完成：听到连续 440Hz 正弦波就说明独占链路通了")
            except Exception as exc:
                print("✘ 自测失败：%r" % exc)
            return None

        ok, why = usbdac.have_tools()
        if not ok:
            print("⚠️ " + why)
            return None
        devs = usbdac.list_devices()
        if not devs:
            print("⚠️ 没有检测到 USB 设备 —— 小尾巴插好了吗？")
            return None
        print("  即将播放 3 秒正弦波（48kHz / 24bit），请把音量调小一点")
        print("  ⚠️ 如果弹出授权窗口，请点【确定】")
        env = dict(os.environ)
        env.update({'USBSINK_MODE': 'tone', 'USBSINK_RATE': '48000',
                    'USBSINK_BITS': '24', 'USBSINK_CH': '2',
                    'USBSINK_BUF': '0.5', 'USBSINK_SECONDS': '3'})
        try:
            r = subprocess.run(['termux-usb', '-r', '-E', '-e', usbdac.LAUNCH, devs[0]],
                               capture_output=True, text=True, timeout=60, env=env)
            out = ((r.stdout or '') + (r.stderr or '')).strip()
        except subprocess.TimeoutExpired:
            print("✘ 自测超时（可能没点确定）")
            return None
        print()
        for line in out.splitlines()[-8:]:
            print("  " + line)
        if '正被内核驱动' in out or '占用' in out:
            print("△ 接口被系统（snd-usb-audio）占着，安全模式下不会抢占。")
            print("  Android 通常会一直绑定这块声卡，所以真要独占只能用: set-usb on force")
            print("  force 模式会先断开内核驱动，退出时会 USBDEVFS_CONNECT 绑回去；")
            print("  仍属有风险操作，建议先确认 set-usb probe 能正确识别拓扑。")
        elif 'Device or resource busy' in out or 'No such device' in out:
            print("✘ 设备被占用或状态残留：把小尾巴拔下来再插上，然后重新 set-usb test")
        elif r.returncode == 0:
            print("✔ 自测完成：听到连续 440Hz 正弦波就说明独占链路通了")
        return None

    # ---------- 只探测，不启用 ----------
    if sub == 'probe':
        ok, text = usbdac.probe()
        print()
        for line in (text or '').splitlines():
            print("  " + line)
        if not ok:
            print("✘ 探测失败（可能需要点弹窗的确定，或拔插一次小尾巴）")
        return None

    # ---------- 纯检测 ----------
    from utils import usbaudio
    cfg = usbdac.load_config()
    devs = usbdac.list_devices() if usbdac.have_tools()[0] else []

    print()
    print("=" * 55)
    print("🎧 USB 音频 / 独占输出检测")
    print("=" * 55)
    from utils import exclusive
    ok_ex, why_ex = exclusive.available()
    backend = exclusive.detect_backend()
    print(f"  平台   : {sys.platform} / {usbdac.platform_note()}")
    print(f"  版本   : {usbdac.version_line()}")
    print(f"  后端   : {exclusive.backend_note()}"
          + ('' if ok_ex else f'  ⚠️ {why_ex}'))
    print(f"  Root   : {'是' if usbaudio.is_root() else '否（本方案不需要 root）'}")

    if backend == 'termux':
        print(f"  termux-usb: {'有' if usbdac.have_tools()[0] else '无'}")
    else:
        mpv = exclusive.mpv_path()
        print(f"  mpv    : {mpv or '未安装（独占输出必须用 mpv）'}")

    if backend != 'termux':
        mdevs = exclusive.list_mpv_devices()
        cfgx = exclusive.get_config() or {}
        print(f"  直通设备: {len(mdevs)} 个" + ('（usblist 看全部）' if mdevs else ''))
        for did, desc in mdevs[:5]:
            mark = ' ←当前' if did == cfgx.get('device') else ''
            print(f"           {did} ({desc}){mark}")
        devs = []
    if devs:
        print(f"  USB 设备: {len(devs)} 个")
        rows = _device_rows()
        for i, r in enumerate(rows, 1):
            mark = ' ←当前' if r['selected'] else ''
            print(f"           {i}. {r['path']}{mark}")
            if r['name']:
                print(f"              身份: {r['name']}")
    elif backend == 'termux':
        print("  USB 设备: 未检测到（小尾巴插好了吗？）")

    prof = usbdac.get_profile(cfg.get('device') or None) if backend == 'termux' else None
    if prof:
        print(f"  设备档案 : {prof.get('vid')}:{prof.get('pid')}  {prof.get('product') or '未知'}"
              f"  {'UAC2' if prof.get('uac2') else 'UAC1'}")
        print(f"             流接口 {prof.get('as_iface')} / 端点 0x{prof.get('ep_out', 0):02x}"
              f" / 时钟源 {prof.get('clock_id')} / 音量控制 "
              f"{'有' if prof.get('has_volume') else '无'}")
        alts = prof.get('alts') or []
        if alts:
            print("             " + ' / '.join(
                '%dbit alt%d(%dB/ms)' % (a.get('bits', 0), a.get('alt', 0), a.get('mps', 0))
                for a in alts))

    if backend == 'termux':
        sink_ok = os.path.isfile(usbdac.SINK)
        print(f"  usbsink  : {'已编译 ' + usbdac.SINK if sink_ok else '未编译（跑 tools/build.sh）'}")

    if cfg.get('enabled') and cfg.get('device'):
        print(f"  独占输出 : 已开启 -> {cfg['device']}")
        print(f"             位深/速率按每首歌自动选（16bit 走 16bit alt，不无谓升位深）")
    else:
        print("  独占输出 : 未开启")

    print(f"  极致完美 : {'已开启（源格式直送，float32/32bit，不重采样）' if usbdac.perfect_enabled() else '未开启'}")
    vmode = '硬件音量（50 刻度）' if usbdac.get_volume_mode() == 'hw' else '软件音量（float 增益）'
    print(f"  音量     : {usbdac.volume_percent()}%（{vmode}）")

    if ctx.player is not None and getattr(ctx.player, 'usb', None) is not None:
        info = ctx.player.usb_info() or {}
        print(f"  当前播放 : {info.get('rate')} Hz / {info.get('bits')} bit（等时流已激活）")

    if backend == 'termux':
        print("  提示   : usb on 开启（会弹一次授权窗）；usb off 回退；usb probe 只看能力")
    else:
        print("  提示   : usb on 直接抢硬件；usblist / usbuse 选设备；usb test 放正弦波")
    print("=" * 55)
    print()
    return None
