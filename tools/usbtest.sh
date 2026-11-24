#!/data/data/com.termux/files/usr/bin/sh
# USB 独占硬件自测：不经过播放器，直接放正弦波
# 用法: bash tools/usbtest.sh [采样率] [位深] [秒数]
#   例: bash tools/usbtest.sh 48000 24 3
#       bash tools/usbtest.sh 44100 16 3
# 拓扑参数会自动从 usb_devices.json 缓存里取（探测过一次就不再写死）
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
RATE="${1:-48000}"
BITS="${2:-24}"
SECS="${3:-3}"
LAUNCHER="$HOME/audioplayer_usb/bin/usbsink-launch"

[ -x "$LAUNCHER" ] || { echo "缺少 $LAUNCHER，先跑 bash tools/build.sh"; exit 1; }

DEV="$(termux-usb -l | grep -o '"/dev/bus/usb/[^"]*"' | head -n 1 | tr -d '"')"
[ -n "$DEV" ] || { echo "没有检测到 USB 设备（小尾巴插好了吗？）"; exit 1; }

TOPO="$(cd "$SRC" && python3 -c "
import sys
sys.path.insert(0, '.')
from utils import usbdac
print(usbdac.UsbSink(device='$DEV').topo_args())
" 2>/dev/null || true)"

echo "设备: $DEV"
echo "格式: $RATE Hz / $BITS bit / 2ch / $SECS 秒正弦波"
[ -n "$TOPO" ] && echo "拓扑: $TOPO"
echo "如果屏幕弹窗，请点【确定】"
# 想测强制模式：FORCE=1 bash tools/usbtest.sh 48000 24 3
[ "${FORCE:-0}" = "1" ] && export USBSINK_FORCE=1 && echo "（force 模式：会短暂接管内核驱动）"
USBSINK_MODE=tone USBSINK_RATE="$RATE" USBSINK_BITS="$BITS" \
USBSINK_SECONDS="$SECS" USBSINK_FREQ="${FREQ:-440}" USBSINK_TOPO="$TOPO" \
    termux-usb -r -E -e "$LAUNCHER" "$DEV"
