"""跨平台兼容性自检：Windows / 桌面 Linux / Termux 三边共存的体检报告。

设计原则（也是这个文件在验证的东西）：
  1. 播放器主体（core/ module/ utils/ 除 usbdac 外的部分）不依赖任何 Termux 专有接口
  2. USB 独占是"可选插件"：usbdac 缺什么能力就降级什么，永远不抛异常
  3. 状态文件（progress.json / scan_cache.json / usb_devices.json）是纯字符串，
     跨平台读得到就读、读不到就忽略，不会因为换了系统就崩
"""

import os
import sys


def _line(name, status, note=''):
    return (name, status, note)


def platform_matrix():
    """返回 [(项目, 状态, 说明)]，状态用 'OK' / 'WARN' / 'NO'。"""
    from utils import usbdac as u

    rows = []
    rows.append(_line('操作系统', 'OK', u.platform_note()))
    rows.append(_line('Python', 'OK', '%s  (%s)' % (sys.version.split()[0], sys.executable)))

    from utils import exclusive as x
    b = x.detect_backend()
    okb, whyb = x.available()
    rows.append(_line('独占输出后端', 'OK' if okb else 'NO', x.backend_note()))
    rows.append(_line('独占输出可用', 'OK' if okb else 'WARN', whyb if not okb else '可用'))

    ok, why = u.usb_supported()
    rows.append(_line('usbfs 独占（Android）', 'OK' if ok else 'NO',
                      '可用' if ok else why))
    okh, whyh = x.hw_volume_supported()
    rows.append(_line('硬件音量', 'OK' if okh else 'WARN', whyh))

    rows.append(_line('termux-usb', 'OK' if u.HAS_TERMUX_USB else 'NO',
                      '存在' if u.HAS_TERMUX_USB else '不存在（非 Termux 环境正常现象）'))
    rows.append(_line('编译好的工具', 'OK' if u.have_tools()[0] else 'WARN',
                      u.BIN_DIR if u.have_tools()[0] else u.have_tools()[1]))
    rows.append(_line('mkfifo（控制管道）', 'OK' if u.HAS_MKFIFO else 'NO',
                      '可用' if u.HAS_MKFIFO else '不可用（硬件音量热改失效）'))
    rows.append(_line('SIGUSR1/2（暂停续播）',
                      'OK' if (u._SIG_PAUSE and u._SIG_RESUME) else 'WARN',
                      '可用' if (u._SIG_PAUSE and u._SIG_RESUME) else 'Windows 无此信号，暂停退化为重启进程'))

    from core.config import FFPLAY_PATH, FFPROBE_PATH
    rows.append(_line('播放器', 'OK' if FFPLAY_PATH else 'NO', FFPLAY_PATH or '找不到 mpv/ffplay'))
    rows.append(_line('ffprobe', 'OK' if FFPROBE_PATH else 'WARN', FFPROBE_PATH or '未找到（有 mutagen 也能跑）'))

    try:
        import mutagen  # noqa: F401
        rows.append(_line('mutagen', 'OK', '已安装（扫描提速 10 倍）'))
    except Exception:
        rows.append(_line('mutagen', 'WARN', '未安装，扫描回退 ffprobe'))

    try:
        import json
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              'progress.json'), 'r', encoding='utf-8') as f:
            d = json.load(f)
        st = d.get('settings') or {}
        rows.append(_line('progress.json', 'OK',
                          'settings=%d 项，进度=%s' % (len(st), '有' if d.get('file') else '无')))
    except Exception as exc:
        rows.append(_line('progress.json', 'WARN', '读不到：%s（会自动新建）' % exc))
    return rows


def simulate():
    """模拟"别的平台"跑一遍关键路径，确认都只是降级、不炸。"""
    from utils import usbdac as u
    results = []

    # 1) 没有 termux-usb（Windows / 桌面 Linux）
    bak = u.HAS_TERMUX_USB
    u.HAS_TERMUX_USB = False
    try:
        ok, why = u.usb_supported()
        keep = u.get_forced_device()
        results.append(('模拟无 termux-usb',
                        'OK' if (not ok and keep is None) else 'NO',
                        '独占自动关闭，播放走系统输出'))
    except Exception as exc:
        results.append(('模拟无 termux-usb', 'NO', repr(exc)))
    finally:
        u.HAS_TERMUX_USB = bak

    # 2) 没有 mkfifo
    bak2 = u.HAS_MKFIFO
    u.HAS_MKFIFO = False
    try:
        ok, why = u.usb_supported()
        sink = u.UsbSink(device='/dev/bus/usb/000/000')
        setret = sink.set_volume(50)
        results.append(('模拟无 mkfifo',
                        'OK' if (not ok and setret is False) else 'NO',
                        '硬件音量写不出去但返回 False，不抛异常'))
    except Exception as exc:
        results.append(('模拟无 mkfifo', 'NO', repr(exc)))
    finally:
        u.HAS_MKFIFO = bak2

    # 3) 没有 SIGUSR1/SIGUSR2（Windows）
    bak3, bak4 = u._SIG_PAUSE, u._SIG_RESUME
    u._SIG_PAUSE = u._SIG_RESUME = None
    try:
        sink = u.UsbSink(device='/dev/bus/usb/000/000')
        results.append(('模拟无 SIGUSR1/2',
                        'OK' if (sink.pause() is False and sink.resume() is False) else 'NO',
                        'pause/resume 返回 False，不会 AttributeError'))
    except Exception as exc:
        results.append(('模拟无 SIGUSR1/2', 'NO', repr(exc)))
    finally:
        u._SIG_PAUSE, u._SIG_RESUME = bak3, bak4

    # 4) 别的平台留下的绝对路径（/sdcard/... 在 Windows 上不存在）
    try:
        from core.scanner import get_audio_metadata
        meta = get_audio_metadata('/sdcard/definitely/not/here.mp3')
        results.append(('跨平台残留路径', 'OK' if not meta else 'NO',
                        '读不到元数据返回 None，走"跳过该曲"分支'))
    except Exception as exc:
        results.append(('跨平台残留路径', 'NO', repr(exc)))

    # 5) 模拟另外两个后端（Linux ALSA / Windows WASAPI）
    real = u.__dict__  # noqa: F841  占位，避免误删变量
    import utils.exclusive as x
    bak_backend = x.detect_backend
    for name, bk, want in (('模拟 Linux ALSA 后端', 'alsa', '--ao=alsa'),
                           ('模拟 Windows WASAPI 后端', 'wasapi', '--ao=wasapi')):
        x.detect_backend = lambda b=bk: b
        try:
            x.save_config(True, device='dummy/hw:1,0')
            args = x.mpv_args(perfect=True, rate=44100)
            hit = any(a.startswith(want) for a in args)
            results.append((name, 'OK' if hit else 'NO',
                            '参数：' + ' '.join(args) if hit else '没生成独占参数'))
        except Exception as exc:
            results.append((name, 'NO', repr(exc)))
        finally:
            x.save_config(False)
    x.detect_backend = bak_backend

    # 6) 拓扑档案缺失（换了新电脑，没有 usb_devices.json）
    try:
        prof = u.get_profile('/dev/bus/usb/000/000')
        results.append(('无拓扑缓存', 'OK' if prof and prof.get('alts') else 'NO',
                        '回落到内置档案 %s' % (prof or {}).get('product', '?')))
    except Exception as exc:
        results.append(('无拓扑缓存', 'NO', repr(exc)))
    return results


def report(verbose=True):
    rows = platform_matrix()
    sims = simulate()

    print()
    print('=' * 62)
    print('🧩 跨平台兼容自检（Windows / Linux / Termux）')
    print('=' * 62)
    for name, status, note in rows:
        flag = {'OK': '✔', 'WARN': '△', 'NO': '✘'}.get(status, '·')
        print(f'  {flag} {name:<22} {note}')
    print('-' * 62)
    print('  降级模拟：')
    for name, status, note in sims:
        flag = {'OK': '✔', 'NO': '✘'}.get(status, '·')
        print(f'  {flag} {name:<22} {note}')
    bad = [r for r in rows + sims if r[1] == 'NO']
    print('-' * 62)
    if bad:
        print(f'  结论：有 {len(bad)} 项不满足，其余功能正常')
    else:
        print('  结论：三平台可以共存 —— 主体功能一致，独占输出三端都有（机制不同）')
    print('=' * 62)
    print()
    return 0 if not bad else 1
