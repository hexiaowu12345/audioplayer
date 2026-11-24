#!/data/data/com.termux/files/usr/bin/sh
# usb_guard.sh —— Termux 层面的独占看门狗（兜底中的兜底）
#
# 作用：用户直接把 Termux 从后台划掉、或者 Termux 被系统杀掉时，
#       Python 和它的子进程都可能来不及跑清理，USB 接口会一直被 usbsink 占着。
#       本脚本每 2 秒检查一次：
#         1) Termux 应用进程还在不在（pgrep com.termux）
#         2) 还有没有 usbsink 残留
#       任何一个条件不成立，就先把残留的 usbsink 全部 TERM（它们会自己还接口），
#       3 秒后还在就 KILL，然后自己退出。
#
# 由播放器在启动独占时自动拉起（单实例），也可以手动跑：
#   sh tools/usb_guard.sh
SINK_PAT="audioplayer_usb/bin/usbsink"

# 单实例：已经有 guard 在跑就直接退出
if pgrep -f "usb_guard[.]sh" >/dev/null 2>&1; then
    exit 0
fi

while :; do
    sleep 2

    # 没有任何残留 usbsink 就没什么可看的，退出
    if ! pgrep -f "$SINK_PAT" >/dev/null 2>&1; then
        exit 0
    fi

    # Termux 主进程还在吗？
    if pgrep -f "com[.]termux" >/dev/null 2>&1; then
        continue
    fi

    # Termux 没了：清理残留
    echo "[usb_guard] Termux 进程已消失，清理残留 usbsink" >&2
    pkill -TERM -f "$SINK_PAT" 2>/dev/null
    sleep 3
    if pgrep -f "$SINK_PAT" >/dev/null 2>&1; then
        echo "[usb_guard] 仍有残留，强制结束（内核会在 fd 关闭时释放接口）" >&2
        pkill -KILL -f "$SINK_PAT" 2>/dev/null
    fi
    exit 0
done
