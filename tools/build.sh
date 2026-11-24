#!/data/data/com.termux/files/usr/bin/bash
# 编译 USB 独占输出组件（源码在 /sdcard 上不能直接执行，必须编译到 $HOME）
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="$HOME/audioplayer_usb/bin"
mkdir -p "$OUT"
LIBUSB="$(pkg-config --cflags --libs libusb-1.0 2>/dev/null || echo '-lusb-1.0')"
gcc -O2 -Wall -o "$OUT/usbprobe" "$SRC/usbprobe.c" $LIBUSB
gcc -O2 -Wall -o "$OUT/usbsink"  "$SRC/usbsink.c" -lm -lpthread
cat > "$OUT/usbsink-launch" << 'LAUNCH'
#!/data/data/com.termux/files/usr/bin/sh
# termux-usb -e 只能带一个命令、不能带参数，这里把环境变量转成 usbsink 的参数
#
# 底层看门狗：如果播放器进程（USBSINK_WATCH）消失 —— 包括用户直接把 Termux
# 从后台划掉、或 Termux 被系统杀掉、Python 来不及跑任何清理 —— 这个 shell 循环
# 会立刻把 usbsink 干掉，让它走 release_iface() 把接口还给内核驱动。
BIN="$HOME/audioplayer_usb/bin/usbsink"
SELF=$$
if [ -n "$USBSINK_WATCH" ] && [ "$USBSINK_WATCH" -gt 0 ] 2>/dev/null; then
  (
    while kill -0 "$USBSINK_WATCH" 2>/dev/null && kill -0 "$SELF" 2>/dev/null; do
      sleep 1
    done
    if kill -0 "$SELF" 2>/dev/null; then
      echo "[看门狗/shell] 播放器已退出，正在结束独占进程 $SELF" >&2
      kill -TERM "$SELF" 2>/dev/null
      sleep 3
      kill -KILL "$SELF" 2>/dev/null
    fi
  ) &
fi
SEC=""
[ -n "$USBSINK_SECONDS" ] && SEC="--seconds $USBSINK_SECONDS"
FRQ=""
[ -n "$USBSINK_FREQ" ] && FRQ="--freq $USBSINK_FREQ"
CTL=""
[ -n "$USBSINK_CTL" ] && CTL="--ctl $USBSINK_CTL"
VOL=""
[ -n "$USBSINK_VOL" ] && VOL="--vol $USBSINK_VOL"
exec "$BIN" "${USBSINK_MODE:-play}" \
    --rate "${USBSINK_RATE:-48000}" \
    --bits "${USBSINK_BITS:-24}" \
    --ch "${USBSINK_CH:-2}" \
    --buf "${USBSINK_BUF:-0.5}" $SEC $FRQ $CTL $VOL ${USBSINK_TOPO:-} \
    ${USBSINK_WATCH:+--watch-pid "$USBSINK_WATCH"} ${USBSINK_FORCE:+--force 1}
LAUNCH
chmod +x "$OUT/usbsink-launch"
echo "已编译到: $OUT"
ls -l "$OUT"
