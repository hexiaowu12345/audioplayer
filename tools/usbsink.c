/* usbsink.c - Termux 下的 USB 音频独占输出进程（无 root，走 usbfs ioctl）
 *
 * 原理：
 *   termux-usb 申请到设备后把 /dev/bus/usb/x/y 的 fd 交给本进程（TERMUX_USB_FD），
 *   本进程直接对这个 fd 发 USBDEVFS_* ioctl：
 *     1) CLAIMINTERFACE 抢下播放流接口（内核 snd-usb-audio 被请走 -> 真正独占）
 *     2) 设置 UAC2 时钟源采样率（CS_SAM_FREQ_CONTROL SET_CUR）
 *     3) 用等时(isochronous) URB 每 1ms 送一包 PCM，完全绕过 AudioFlinger
 *
 * 用法：
 *   usbsink probe
 *   usbsink tone --rate 48000 --bits 24 --seconds 3
 *   usbsink play --rate 48000 --bits 24            # 从 stdin 读裸 PCM
 *   可选：--ch 2 --urb 4 --frames 8 --gain 1.0 --freq 440
 *   暂停/续播：SIGUSR1 = 断开流(alt 0)，SIGUSR2 = 重新打开
 *
 * 本机实测（CX31993+MAX97220, 06cb:1594, UAC2）：
 *   AC 接口=0，时钟源 ClockID=0x09，播放流接口=1，播放端点=0x01（等时 OUT）
 *   alt1=16bit(包192) alt2=24bit(包288) alt3=32bit(包384)
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <math.h>
#include <time.h>
#include <fcntl.h>
#include <sys/prctl.h>
#include <sys/ioctl.h>
#include <linux/usbdevice_fs.h>

static int g_iface = 1;      /* 播放流接口 */
static int g_ac_iface = 0;
static int g_clock = 0x09;
static int g_ep_out = 0x01;
static int g_uac2 = 1;       /* 1=UAC2(时钟源实体) 0=UAC1(端点采样率) */
static int g_has_vol = 1;
static int g_watch_pid = 0;   /* 播放器进程 pid：它一没，本进程立刻收摊还接口 */
static int g_force = 0;      /* 1=强制从内核驱动手里抢接口（危险，需显式授权） */
static int g_claimed = 0;    /* 我们真的接管了接口吗（决定退出时要不要还） */
static int g_disconnected = 0; /* 是否执行过 DISCONNECT（决定要不要 CONNECT 回去） */
/* force 模式下要一起断开的接口：只断 AS 不管 AC，会让 snd-usb-audio
   处于"半死"状态（声卡还在、PCM 没了），Android 音频 HAL 会卡死。
   Poweramp 这类播放器是把整个设备的驱动都请走，声卡干净消失，系统自动切扬声器。 */
static int g_extra_claimed[4] = {-1, -1, -1, -1};
static int g_nextra = 0;
static int g_iflist[6] = {-1, -1, -1, -1, -1, -1};
static int g_nif = 0;
static int g_disconnected_list[6];
static int g_ndisc = 0;
/* 子帧字节数 -> alt 号 / 单包上限，播放器按探测结果填 */
static int g_alt_map[5] = {-1, -1, 1, 2, 3};
static int g_mps_map[5] = {0, 0, 192, 288, 384};

static int g_fd = -1;
static volatile sig_atomic_t g_paused = 0;
static volatile sig_atomic_t g_quit = 0;

static int usb_control(uint8_t reqtype, uint8_t req, uint16_t value,
                       uint16_t index, void *data, uint16_t len, int timeout_ms)
{
    struct usbdevfs_ctrltransfer ct;
    memset(&ct, 0, sizeof ct);
    ct.bRequestType = reqtype;
    ct.bRequest     = req;
    ct.wValue       = value;
    ct.wIndex       = index;
    ct.wLength      = len;
    ct.timeout      = timeout_ms;
    ct.data         = data;
    int r = ioctl(g_fd, USBDEVFS_CONTROL, &ct);
    if (r < 0) return -errno;
    return r;
}

static int set_alt(int alt);
static void release_iface(void);

/* 采样率/音量这类"类请求"是发给控制接口(AC)的，
   usbfs 要求该接口也被本进程 claim，否则返回 ENOENT。
   以前没 claim AC，所以永远是"设置采样率失败 / 音量 Broken pipe"。 */
static int g_cap_iface = -1;

static void claim_aux_ifaces(void)
{
    if (g_ac_iface < 0 || g_ac_iface == g_iface) return;
    int ifno = g_ac_iface;
    if (ioctl(g_fd, USBDEVFS_CLAIMINTERFACE, &ifno) == 0) {
        g_extra_claimed[g_nextra++] = ifno;
        fprintf(stderr, "[信息] 控制接口 %d 也已接管（采样率/音量控制需要）\n", ifno);
    } else {
        fprintf(stderr, "[警告] 控制接口 %d 接管失败: %s（采样率可能改不动）\n",
                ifno, strerror(errno));
    }
    /* 录音接口也顺手接管：内核按 wIndex 低字节校验接口号，
       音量请求的实体号 2 恰好等于这个接口号，接管后就能过网关 */
    if (g_cap_iface >= 0 && g_cap_iface != g_iface && g_cap_iface != g_ac_iface) {
        int cap = g_cap_iface;
        if (ioctl(g_fd, USBDEVFS_CLAIMINTERFACE, &cap) == 0)
            g_extra_claimed[g_nextra++] = cap;
    }
}

/* 接管接口：先试普通 claim；失败且允许强制时，把 snd-usb-audio 从
   整个设备的接口上请走（AC + AS + HID 都要断，半断会搞哑系统音频）。 */
static int claim_iface(void)
{
    /* 刚有别的进程（比如预热用的 mpv）放开设备时，内核可能还没完全放手，
       这里重试几次，别一失败就退。 */
    int trial = -1;
    for (int attempt = 0; attempt < 4; attempt++) {
        trial = ioctl(g_fd, USBDEVFS_CLAIMINTERFACE, &g_iface);
        if (trial == 0) break;
        if (errno != EBUSY) break;
        if (attempt < 3) usleep(250000);
    }
    if (trial == 0) {
        g_claimed = 1;
        claim_aux_ifaces();
        return 0;
    }
    int err = errno;
    if (err != EBUSY) return -err;

    if (!g_force) {
        fprintf(stderr, "[安全] 接口 %d 正被内核驱动(snd-usb-audio)占用。\n", g_iface);
        fprintf(stderr, "[安全] 直接抢占有可能让系统音频卡死（要拔插+重启才能恢复），\n");
        fprintf(stderr, "[安全] 已停止，未对设备做任何修改。要冒险请用: set-usb on force\n");
        return 1;
    }

    int n = (g_nif > 0) ? g_nif : 1;
    if (g_nif <= 0) g_iflist[0] = g_iface;
    for (int i = 0; i < n; i++) {
        int ifno = g_iflist[i];
        if (ifno < 0) continue;
        struct usbdevfs_disconnect_claim dc;
        memset(&dc, 0, sizeof dc);
        dc.interface = (unsigned int)ifno;
        dc.flags = USBDEVFS_DISCONNECT_CLAIM_IF_DRIVER;   /* 只断 snd-usb-audio */
        snprintf((char *)dc.driver, sizeof dc.driver, "snd-usb-audio");
        if (ioctl(g_fd, USBDEVFS_DISCONNECT_CLAIM, &dc) == 0) {
            g_disconnected_list[g_ndisc++] = ifno;
            fprintf(stderr, "[警告] 已断开内核驱动与接口 %d 的绑定\n", ifno);
        } else {
            /* 老内核没有 DISCONNECT_CLAIM：退回 DISCONNECT */
            if (ioctl(g_fd, USBDEVFS_DISCONNECT, &ifno) == 0)
                g_disconnected_list[g_ndisc++] = ifno;
        }
    }
    if (g_ndisc == 0) {
        /* 没有可断开的驱动（比如上一次已经把驱动断掉了），
           那接口可能就是自由的，直接 claim 试试 */
        if (ioctl(g_fd, USBDEVFS_CLAIMINTERFACE, &g_iface) == 0) {
            g_claimed = 1;
            claim_aux_ifaces();
            fprintf(stderr, "[信息] 接口本来就空闲，直接接管成功\n");
            return 0;
        }
        fprintf(stderr, "[安全] 接管不到接口（%s），放弃（未做修改）\n", strerror(errno));
        return 1;
    }
    /* 断开后重新 claim 我们真正要用的那个。
       DISCONNECT_CLAIM 成功时接口其实已经归我们了，再 claim 可能返回
       EALREADY/EINVAL，这不算失败。 */
    if (ioctl(g_fd, USBDEVFS_CLAIMINTERFACE, &g_iface) < 0) {
        int e = errno;
        if (e == EALREADY || e == EINVAL || e == EBUSY) {
            fprintf(stderr, "[信息] 接口 %d 已由 DISCONNECT_CLAIM 接管\n", g_iface);
        } else {
            fprintf(stderr, "[错误] 断开驱动后仍 claim 不到接口 %d: %s\n",
                    g_iface, strerror(e));
            release_iface();
            return -e;
        }
    }
    g_claimed = 1;
    g_disconnected = (g_ndisc > 0);
    claim_aux_ifaces();
    fprintf(stderr, "[警告] 已强制接管接口（系统音频会短暂切到扬声器）\n");
    return 0;
}

/* 归还接口：alt 0 -> release -> 把驱动一个个绑回去。任何退出路径都要走一遍。 */
static void release_iface(void)
{
    if (g_claimed) {
        set_alt(0);
        ioctl(g_fd, USBDEVFS_RELEASEINTERFACE, &g_iface);
        g_claimed = 0;
        fprintf(stderr, "[收尾] 接口 %d 已归还\n", g_iface);
    }
    for (int i = g_nextra - 1; i >= 0; i--) {
        int ifno = g_extra_claimed[i];
        ioctl(g_fd, USBDEVFS_RELEASEINTERFACE, &ifno);
        g_extra_claimed[i] = -1;
    }
    g_nextra = 0;
    for (int i = g_ndisc - 1; i >= 0; i--) {
        int ifno = g_disconnected_list[i];
        if (ioctl(g_fd, USBDEVFS_CONNECT, &ifno) == 0)
            fprintf(stderr, "[收尾] 内核驱动已重新绑定接口 %d\n", ifno);
        else
            fprintf(stderr, "[收尾] 接口 %d 重新绑定失败(%s)，建议拔插一次小尾巴\n",
                    ifno, strerror(errno));
    }
    g_ndisc = 0;
    g_disconnected = 0;
}

static int set_alt(int alt)
{
    struct usbdevfs_setinterface si;
    si.interface  = g_iface;
    si.altsetting = alt;
    if (ioctl(g_fd, USBDEVFS_SETINTERFACE, &si) < 0) return -errno;
    return 0;
}

/* ===================== 硬件音量（UAC2 Feature Unit） ===================== */
static int g_vol_unit = 0x02;      /* 描述符里 Feature Unit ID = 2 */
static int g_vol_min = 0, g_vol_max = 0, g_vol_res = 1, g_vol_ok = 0;
static int g_vol_pct = 100;
static int g_ctl_fd = -1;

static int volume_probe(void)
{
    uint8_t b[6] = {0, 0, 0, 0, 0, 0};
    int r = usb_control(0xA1, 0x82, 0x0200,
                        (uint16_t)((g_ac_iface << 8) | g_vol_unit), b, 6, 1000);
    if (r < 0) return r;
    g_vol_min = (int16_t)(b[0] | (b[1] << 8));
    g_vol_max = (int16_t)(b[2] | (b[3] << 8));
    g_vol_res = (int16_t)(b[4] | (b[5] << 8));
    if (g_vol_res <= 0) g_vol_res = 1;
    g_vol_ok = 1;
    return 0;
}

static int volume_get(void)
{
    uint8_t b[2] = {0, 0};
    int r = usb_control(0xA1, 0x81, 0x0200,
                        (uint16_t)((g_ac_iface << 8) | g_vol_unit), b, 2, 1000);
    if (r < 0) return r;
    return (int16_t)(b[0] | (b[1] << 8));
}

/* 0~100% -> 1/256 dB 的硬件寄存器值（0 dB 在 max 端） */
static int volume_set_pct(int pct)
{
    if (!g_vol_ok) return -1;
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    long v = (long)g_vol_max + (long)(g_vol_min - g_vol_max) * (long)(100 - pct) / 100;
    if (v > g_vol_max) v = g_vol_max;
    if (v < g_vol_min) v = g_vol_min;
    v = (v / g_vol_res) * g_vol_res;
    uint8_t b[2] = { (uint8_t)(v & 0xFF), (uint8_t)(((uint16_t)v >> 8) & 0xFF) };
    int r = -1;
    /* 依次试：master(cn=0) / 通道1 / 通道2；以及 wIndex 只带实体号的写法。
       有些设备的 Feature Unit 不支持 master 通道，必须逐个通道设。 */
    int idxs[2] = { (g_ac_iface << 8) | g_vol_unit, g_vol_unit };
    for (int k = 0; k < 2 && r < 0; k++) {
        for (int cn = 0; cn <= 2 && r < 0; cn++) {
            r = usb_control(0x21, 0x01, (uint16_t)(0x0200 | cn),
                            (uint16_t)idxs[k], b, 2, 1000);
            if (r >= 0 && cn != 0) {
                /* 记下可用的通道，下次直接用 */
                fprintf(stderr, "[音量] 该设备需要按通道设置（cn=%d）\n", cn);
            }
        }
    }
    if (r < 0) return r;
    g_vol_pct = pct;
    /* 0% 顺便静音，避免极小音量仍有底噪 */
    uint8_t mute = (pct == 0) ? 1 : 0;
    usb_control(0x21, 0x01, 0x0100,
                (uint16_t)((g_ac_iface << 8) | g_vol_unit), &mute, 1, 1000);
    return 0;
}

/* 控制管道：播放器往 FIFO 写一个字节 'V'+百分比，实时改音量 */
static void ctl_pump(void)
{
    if (g_ctl_fd < 0) return;
    uint8_t buf[64];
    ssize_t n;
    while ((n = read(g_ctl_fd, buf, sizeof buf)) > 0) {
        for (ssize_t i = 0; i < n; i++) {
            if (buf[i] == 'V' && i + 1 < n) {
                int pct = buf[i + 1];
                i++;
                if (volume_set_pct(pct) == 0)
                    fprintf(stderr, "[音量] 硬件音量 -> %d%%\n", g_vol_pct);
                else
                    fprintf(stderr, "[音量] 设置失败\n");
            }
        }
    }
}

static int clock_get_cur(void)
{
    if (g_uac2) {
        uint32_t v = 0;
        int r = usb_control(0xA1, 0x81, 0x0100,
                            (uint16_t)((g_ac_iface << 8) | g_clock), &v, 4, 1000);
        if (r >= 0) return (int)v;
        unsigned char b[3] = {0, 0, 0};
        r = usb_control(0xA2, 0x81, 0x0100, (uint16_t)g_ep_out, b, 3, 1000);
        if (r < 0) return r;
        return (int)(b[0] | (b[1] << 8) | (b[2] << 16));
    } else {
        unsigned char b[3] = {0, 0, 0};
        int r = usb_control(0xA2, 0x81, 0x0100, (uint16_t)g_ep_out, b, 3, 1000);
        if (r < 0) return r;
        return (int)(b[0] | (b[1] << 8) | (b[2] << 16));
    }
}

static int clock_get_range(uint32_t *lo, uint32_t *hi, uint32_t *res)
{
    if (g_uac2) {
        uint32_t b[3] = {0, 0, 0};
        int r = usb_control(0xA1, 0x82, 0x0100,
                            (uint16_t)((g_ac_iface << 8) | g_clock), b, 12, 1000);
        if (r < 0) return r;
        *lo = b[0]; *hi = b[1]; *res = b[2];
        return 0;
    } else {
        unsigned char b[6] = {0, 0, 0, 0, 0, 0};
        int r = usb_control(0xA2, 0x82, 0x0100, (uint16_t)g_ep_out, b, 6, 1000);
        if (r < 0) return r;
        *lo = b[0] | (b[1] << 8) | (b[2] << 16);
        *hi = b[3] | (b[4] << 8) | (b[5] << 16);
        *res = 0;
        return 0;
    }
}

static int clock_set_cur_uac1(uint32_t rate);
static int clock_set_cur(uint32_t rate);

static int clock_set_cur(uint32_t rate)
{
    if (g_uac2) {
        int r = usb_control(0x21, 0x01, 0x0100,
                            (uint16_t)((g_ac_iface << 8) | g_clock), &rate, 4, 1000);
        if (r >= 0) return r;
        /* 内核的 usbfs 会把 wIndex 低字节当成接口号去校验（UAC2 这里是实体号），
           于是返回 ENOENT。退回 UAC1 的"端点采样率"写法，很多 UAC2 设备也认。 */
        r = clock_set_cur_uac1(rate);
        if (r >= 0) {
            fprintf(stderr, "[信息] 采样率改用端点式请求设置成功\n");
            return r;
        }
        return -1;
    }
    return clock_set_cur_uac1(rate);
}

static int clock_set_cur_uac1(uint32_t rate)
{
    unsigned char b[3] = { (unsigned char)(rate & 0xFF),
                           (unsigned char)((rate >> 8) & 0xFF),
                           (unsigned char)((rate >> 16) & 0xFF) };
    return usb_control(0x22, 0x01, 0x0100, (uint16_t)g_ep_out, b, 3, 1000);
}

static uint8_t *ring;
static size_t ring_cap, ring_rd, ring_wr, ring_cnt;
static pthread_mutex_t ring_mx = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  ring_cv = PTHREAD_COND_INITIALIZER;
static int g_eof = 0;
static unsigned long g_underrun_packets = 0, g_bytes_sent = 0;
static double g_gain = 1.0, g_freq = 440.0;
static int g_src_tone = 0;
static double g_phase = 0.0;
static int g_rate = 48000, g_ch = 2, g_bytes = 3;

static void ring_put(const uint8_t *p, size_t n)
{
    pthread_mutex_lock(&ring_mx);
    while (n > 0 && !g_quit) {
        while (ring_cnt == ring_cap && !g_quit)
            pthread_cond_wait(&ring_cv, &ring_mx);
        size_t room = ring_cap - ring_cnt;
        size_t k = n < room ? n : room;
        size_t tail = (ring_wr + k <= ring_cap) ? k : (ring_cap - ring_wr);
        memcpy(ring + ring_wr, p, tail);
        if (k > tail) memcpy(ring, p + tail, k - tail);
        ring_wr = (ring_wr + k) % ring_cap;
        ring_cnt += k;
        p += k; n -= k;
    }
    pthread_mutex_unlock(&ring_mx);
}

static size_t ring_get(uint8_t *dst, size_t want)
{
    pthread_mutex_lock(&ring_mx);
    size_t k = ring_cnt < want ? ring_cnt : want;
    size_t head = (ring_rd + k <= ring_cap) ? k : (ring_cap - ring_rd);
    memcpy(dst, ring + ring_rd, head);
    if (k > head) memcpy(dst + head, ring, k - head);
    ring_rd = (ring_rd + k) % ring_cap;
    ring_cnt -= k;
    pthread_cond_signal(&ring_cv);
    pthread_mutex_unlock(&ring_mx);
    return k;
}

static void *feeder(void *arg)
{
    (void)arg;
    size_t chunk = 8192;
    uint8_t *buf = malloc(chunk);
    fprintf(stderr, "[信息] 模式=%s 输入=%s\n",
            g_src_tone ? "tone(正弦波自测)" : "play(stdin 裸PCM)",
            g_src_tone ? "内部生成" : "stdin");
    if (!g_src_tone) {
        int first = 1;
        while (!g_quit) {
            ssize_t r = read(STDIN_FILENO, buf, chunk);
            if (r <= 0) break;
            if (first && r >= 8) {
                first = 0;
                long long s0 = 0, s1 = 0, s2 = 0, s3 = 0;
                if (g_bytes == 2) {
                    s0 = (short)(buf[0] | (buf[1] << 8));
                    s1 = (short)(buf[2] | (buf[3] << 8));
                    s2 = (short)(buf[4] | (buf[5] << 8));
                    s3 = (short)(buf[6] | (buf[7] << 8));
                }
                double sum = 0;
                int n = 0;
                for (ssize_t i = 0; i + 1 < r && n < 4000; i += 2) {
                    short v = (short)(buf[i] | (buf[i + 1] << 8));
                    sum += (double)v * v;
                    n++;
                }
                double rms = (n > 0) ? sqrt(sum / n) : 0;
                fprintf(stderr, "[数据] 首包样本=%lld,%lld,%lld,%lld RMS=%.0f\n",
                        s0, s1, s2, s3, rms);
            }
            ring_put(buf, (size_t)r);
        }
        fprintf(stderr, "[数据] stdin 结束\n");
    } else {
        while (!g_quit) {
            size_t nf = 512;
            size_t need = nf * (size_t)g_ch * (size_t)g_bytes;
            if (need > chunk) { chunk = need; buf = realloc(buf, chunk); }
            for (size_t i = 0; i < nf; i++) {
                double v = sin(g_phase) * 0.30 * g_gain;
                g_phase += 2.0 * M_PI * g_freq / (double)g_rate;
                if (g_phase > 2 * M_PI) g_phase -= 2 * M_PI;
                int32_t s = (int32_t)lrint(v * 2147483647.0);
                for (int c = 0; c < g_ch; c++) {
                    uint8_t *o = buf + (i * (size_t)g_ch + (size_t)c) * (size_t)g_bytes;
                    if (g_bytes == 2) { int16_t s16 = (int16_t)(s >> 16); memcpy(o, &s16, 2); }
                    else if (g_bytes == 3) { uint32_t u = (uint32_t)s; o[0] = u & 0xFF; o[1] = (u >> 8) & 0xFF; o[2] = (u >> 16) & 0xFF; }
                    else { memcpy(o, &s, 4); }
                }
            }
            ring_put(buf, nf * (size_t)g_ch * (size_t)g_bytes);
        }
    }
    g_eof = 1;
    pthread_mutex_lock(&ring_mx);
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mx);
    free(buf);
    return NULL;
}

typedef struct {
    struct usbdevfs_urb *urb;
    uint8_t *buf;
    int busy;
} urb_slot_t;

static urb_slot_t *slots;
static int g_nurbs = 8, g_fs = 8, g_alt = 2;
static uint64_t g_frame_acc = 0;
/* 发送节拍器：等时流必须严格按 1ms 一包发出去。
   以前只要 URB 完成就立刻再提交，结果是"全速灌"（实测 7.7 倍速），
   设备收到的数据量远超实时，听感就是没声/变调。 */
static double g_t0 = 0.0;
static int g_next_frame = -1;   /* 下一个包应该落在哪一帧（保证连续） */
static int g_frame_errors = 0;  /* 帧号跳变次数（排查断续用） */
static long long g_submitted = 0;

static double now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* 现在"应该"发到第几包了（开头给 3 包余量，避免起播抖动） */
/* 提前量：一直比实时快 24ms 排队，让内核手里始终有几帧余量，
   否则"刚好赶上"的发包方式一旦抖动就会断断续续。 */
#define SEND_LEAD_MS 24.0

static long long packets_due(void)
{
    if (g_t0 <= 0.0) g_t0 = now_sec();
    double el = now_sec() - g_t0 + SEND_LEAD_MS / 1000.0;
    if (el < 0) el = 0;
    return (long long)(el * 1000.0);
}

static int next_packet_bytes(void)
{
    uint64_t num = g_frame_acc + (uint64_t)g_rate;
    int frames = (int)(num / 1000);
    g_frame_acc = num % 1000;
    return frames * g_ch * g_bytes;
}

static int fill_and_submit(urb_slot_t *s)
{
    struct usbdevfs_urb *u = s->urb;
    u->buffer = s->buf;
    u->buffer_length = 0;

    /* 节拍控制：到期的包数 - 已提交 = 这次最多能装几包 */
    long long due = packets_due();
    long long can = due - g_submitted;
    if (can <= 0) return 0;              /* 还没到下一包的时间 */
    int npk = (can < g_fs) ? (int)can : g_fs;

    for (int i = 0; i < npk; i++) {
        int want = next_packet_bytes();
        size_t got = ring_get(s->buf + u->buffer_length, (size_t)want);
        if (got < (size_t)want) {
            memset(s->buf + u->buffer_length + got, 0, (size_t)want - got);
            g_underrun_packets++;
        }
        u->iso_frame_desc[i].length = want;
        u->iso_frame_desc[i].actual_length = 0;
        u->iso_frame_desc[i].status = 0;
        u->buffer_length += want;
    }
    u->number_of_packets = npk;
    u->actual_length = 0;
    u->error_count = 0;
    u->status = 0;
    /* 显式给出起始帧号，包与包之间不留空帧；
       第一包用 ISO_ASAP 让内核给个起点，之后自己续。 */
    if (g_next_frame < 0) {
        u->flags = USBDEVFS_URB_ISO_ASAP;     /* 第一个包让内核给起点 */
    } else {
        u->flags = 0;
        u->start_frame = g_next_frame & 0x7FF;
    }
    if (ioctl(g_fd, USBDEVFS_SUBMITURB, u) < 0) {
        /* 装不下的包数不前进，下次重试 */
        /* 包数不前进，下次重试 */
        return -errno;
    }
    g_submitted += npk;
    if (g_next_frame >= 0) g_next_frame = (g_next_frame + npk) & 0x7FF;
    g_bytes_sent += (unsigned long)u->buffer_length;
    return npk;
}

/* 看门狗：播放器/Termux 被划掉时，Python 来不及做任何清理，
   这里由底层进程自己发现"老大了"，先还接口再退出。 */
static int watch_alive(void)
{
    if (g_watch_pid <= 0) return 1;
    if (kill(g_watch_pid, 0) == 0) return 1;
    if (errno == EPERM) return 1;       /* 存在但没权限发信号，算活着 */
    fprintf(stderr, "[看门狗] 播放器进程 %d 已退出，正在归还 USB 接口并退出\n", g_watch_pid);
    return 0;
}

static void on_signal(int sig)
{
    if (sig == SIGUSR1) g_paused = 1;
    else if (sig == SIGUSR2) g_paused = 0;
    else g_quit = 1;
}

static int ring_empty(void)
{
    pthread_mutex_lock(&ring_mx);
    int e = (ring_cnt == 0);
    pthread_mutex_unlock(&ring_mx);
    return e;
}

static void usage(void)
{
    fprintf(stderr,
        "用法: usbsink probe\n"
        "      usbsink tone --rate 48000 --bits 24 [--freq 440] [--seconds 3]\n"
        "      usbsink play --rate 48000 --bits 24     (裸 PCM 由 stdin 输入)\n"
        "选项: --ch 2  --urb 4  --frames 8  --gain 1.0\n");
}

static const char *get_opt(int argc, char **argv, const char *name)
{
    for (int i = 2; i < argc - 1; i++)
        if (strcmp(argv[i], name) == 0) return argv[i + 1];
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc < 2) { usage(); return 2; }
    const char *mode = argv[1];

    const char *ev = getenv("TERMUX_USB_FD");
    if (ev && *ev) g_fd = atoi(ev);

    const char *s;
    if ((s = get_opt(argc, argv, "--rate")))   g_rate  = atoi(s);
    if ((s = get_opt(argc, argv, "--ch")))     g_ch    = atoi(s);
    if ((s = get_opt(argc, argv, "--bits")))   g_bytes = atoi(s) / 8;
    if ((s = get_opt(argc, argv, "--urb")))    g_nurbs = atoi(s);
    if ((s = get_opt(argc, argv, "--frames"))) g_fs    = atoi(s);
    if ((s = get_opt(argc, argv, "--gain")))   g_gain  = atof(s);
    if ((s = get_opt(argc, argv, "--freq")))   g_freq  = atof(s);
    double seconds = 0;
    if ((s = get_opt(argc, argv, "--seconds"))) seconds = atof(s);
    /* ---- 拓扑参数：由播放器按 usbprobe 探测结果传入，不再写死 ---- */
    if ((s = get_opt(argc, argv, "--ac-iface"))) g_ac_iface = atoi(s);
    if ((s = get_opt(argc, argv, "--as-iface"))) g_iface    = atoi(s);
    if ((s = get_opt(argc, argv, "--ep")))       g_ep_out   = (int)strtol(s, NULL, 0);
    if ((s = get_opt(argc, argv, "--clock")))    g_clock    = atoi(s);
    if ((s = get_opt(argc, argv, "--unit")))     g_vol_unit = atoi(s);
    if ((s = get_opt(argc, argv, "--cap-iface"))) g_cap_iface = atoi(s);
    if ((s = get_opt(argc, argv, "--uac")))      g_uac2     = (atoi(s) == 2);
    if ((s = get_opt(argc, argv, "--has-vol")))  g_has_vol  = atoi(s);
    if ((s = get_opt(argc, argv, "--force")))    g_force    = atoi(s);
    if (!g_force) {
        const char *f = getenv("USBSINK_FORCE");   /* 参数没传到时的兜底 */
        if (f && *f && atoi(f)) g_force = 1;
    }
    if ((s = get_opt(argc, argv, "--ifaces"))) {
        char buf[64];
        snprintf(buf, sizeof buf, "%s", s);
        char *save = NULL;
        char *tok = strtok_r(buf, ", ", &save);
        while (tok && g_nif < 6) {
            g_iflist[g_nif++] = atoi(tok);
            tok = strtok_r(NULL, ", ", &save);
        }
    }
    if ((s = get_opt(argc, argv, "--watch-pid"))) g_watch_pid = atoi(s);
    if ((s = get_opt(argc, argv, "--alt16")))    g_alt_map[2] = atoi(s);
    if ((s = get_opt(argc, argv, "--alt24")))    g_alt_map[3] = atoi(s);
    if ((s = get_opt(argc, argv, "--alt32")))    g_alt_map[4] = atoi(s);
    if ((s = get_opt(argc, argv, "--mps16")))    g_mps_map[2] = atoi(s);
    if ((s = get_opt(argc, argv, "--mps24")))    g_mps_map[3] = atoi(s);
    if ((s = get_opt(argc, argv, "--mps32")))    g_mps_map[4] = atoi(s);

    if (strcmp(mode, "probe") == 0) {
        if (g_fd < 0) { fprintf(stderr, "没有拿到设备 fd（TERMUX_USB_FD）\n"); return 3; }
        int cur = clock_get_cur();
        uint32_t lo = 0, hi = 0, res = 0;
        int rr = clock_get_range(&lo, &hi, &res);
        printf("=== USB 音频时钟探测 ===\n");
        if (cur > 0) printf("当前采样率: %d Hz\n", cur);
        else         printf("当前采样率读取失败: %s\n", strerror(-cur));
        if (rr == 0) {
            printf("支持范围: %u ~ %u Hz  步进 %u\n", lo, hi, res);
            int cand[] = {44100, 48000, 96000, 192000, 384000};
            printf("档位试探:");
            for (unsigned i = 0; i < sizeof(cand) / sizeof(cand[0]); i++) {
                if ((uint32_t)cand[i] < lo || (uint32_t)cand[i] > hi) continue;
                int r = clock_set_cur((uint32_t)cand[i]);
                int back = (r < 0) ? -1 : clock_get_cur();
                printf(" %d%s", cand[i], (back == cand[i]) ? "✓" : "✗");
                fflush(stdout);
            }
            printf("\n");
            if (cur > 0) clock_set_cur((uint32_t)cur);
        } else {
            printf("支持范围读取失败: %s\n", strerror(-rr));
        }
        (void)g_ep_out;
        return 0;
    }

    if (g_bytes != 2 && g_bytes != 3 && g_bytes != 4) { fprintf(stderr, "位深只支持 16/24/32\n"); return 2; }

    /* topo 模式：只回显解析出来的拓扑，不需要设备（方便验证参数传递） */
    if (strcmp(mode, "topo") == 0) {
        printf("ac_iface=%d as_iface=%d ep=0x%02x clock=%d unit=%d uac=%d has_vol=%d\n",
               g_ac_iface, g_iface, g_ep_out, g_clock, g_vol_unit, g_uac2, g_has_vol);
        printf("alt: 16bit=%d(%d) 24bit=%d(%d) 32bit=%d(%d)\n",
               g_alt_map[2], g_mps_map[2], g_alt_map[3], g_mps_map[3],
               g_alt_map[4], g_mps_map[4]);
        printf("play: rate=%d bits=%d ch=%d bytes=%d\n", g_rate, g_bytes * 8, g_ch, g_bytes);
        printf("force=%d ifaces=%d[", g_force, g_nif);
        for (int i = 0; i < g_nif; i++) printf("%s%d", i ? "," : "", g_iflist[i]);
        printf("] watch_pid=%d\n", g_watch_pid);
        return 0;
    }

    if (g_fd < 0) { fprintf(stderr, "没有拿到设备 fd（TERMUX_USB_FD）\n"); return 3; }
    g_src_tone = (strcmp(mode, "tone") == 0);

    signal(SIGUSR1, on_signal);
    signal(SIGUSR2, on_signal);
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);
    prctl(PR_SET_PDEATHSIG, SIGTERM);
    /* 把真实 pid 写出去：播放器暂停/续播的 SIGUSR1/2 必须发给本进程，
       发给 termux-usb（我们 spawn 的那个进程）会直接把它 SIGUSR1 打死，
       连带着 usbsink 因为 PDEATHSIG 一起收摊 —— 之前"一暂停就断开"就是这个。 */
    {
        const char *pf = getenv("USBSINK_PIDFILE");
        if (pf && *pf) {
            FILE *f = fopen(pf, "w");
            if (f) { fprintf(f, "%d\n", (int)getpid()); fclose(f); }
        }
    }       /* 父进程(termux-usb)没了就收到 SIGTERM */
    if (g_watch_pid > 0)
        fprintf(stderr, "[看门狗] 监视播放器进程 %d\n", g_watch_pid);

    int r = claim_iface();
    if (r > 0) return 7;          /* 被占用且未强制：交给播放器回退系统输出 */
    if (r < 0) {
        fprintf(stderr, "[错误] claim 接口 %d 失败: %s\n", g_iface, strerror(-r));
        return 7;
    }
    fprintf(stderr, "[信息] 接口 %d 已独占\n", g_iface);

    int cur = clock_get_cur();
    if (cur > 0) fprintf(stderr, "[信息] 原采样率 %d Hz\n", cur);
    r = clock_set_cur((uint32_t)g_rate);
    if (r < 0) fprintf(stderr, "[警告] 设置采样率失败: %s\n", strerror(-r));
    cur = clock_get_cur();
    fprintf(stderr, "[信息] 采样率 = %d Hz（设备读回 %d）\n", g_rate, cur);
    /* 设备不接受这个采样率的话，宁可明确报错让播放器回退，也不要变速播放 */
    if (cur > 0 && cur != g_rate) {
        fprintf(stderr, "[错误] 设备不接受 %d Hz（实际是 %d Hz），已放弃独占输出\n",
                g_rate, cur);
        release_iface();
        return 6;
    }

    g_alt = (g_bytes >= 2 && g_bytes <= 4) ? g_alt_map[g_bytes] : -1;
    if (g_alt < 0) {
        fprintf(stderr, "[错误] 设备没有 %d bit 的播放 alt 设置\n", g_bytes * 8);
        release_iface();
        return 4;
    }
    if (set_alt(g_alt) < 0) {
        fprintf(stderr, "[错误] 切 alt %d 失败: %s\n", g_alt, strerror(errno));
        release_iface();
        return 4;
    }
    fprintf(stderr, "[信息] alt %d 已激活（%d bit / %d ch / %d Hz 等时流）\n",
            g_alt, g_bytes * 8, g_ch, g_rate);

    /* --ctl 控制 FIFO 路径，--vol 初始硬件音量百分比 */
    const char *ctl_path = NULL;
    if ((s = get_opt(argc, argv, "--ctl"))) ctl_path = s;
    if ((s = get_opt(argc, argv, "--vol"))) g_vol_pct = atoi(s);

    /* --buf 秒数：越小暂停/切歌响应越快，越大抗抖动越好（默认 0.5s） */
    double buf_sec = 0.5;
    if ((s = get_opt(argc, argv, "--buf"))) {
        buf_sec = atof(s);
        if (buf_sec < 0.1) buf_sec = 0.1;
        if (buf_sec > 4.0) buf_sec = 4.0;
    }

    /* 硬件音量：先读范围，再设初始值 */
    if (!g_has_vol) {
        fprintf(stderr, "[音量] 描述符里 Feature Unit 没有音量控制，改用软件音量\n");
        r = -1;
    } else {
        r = volume_probe();
    }
    if (r == 0) {
        int cur_vol = volume_get();
        fprintf(stderr, "[音量] 硬件音量可用：范围 %d ~ %d (1/256 dB, %.1f ~ %.1f dB)，当前 %d\n",
                g_vol_min, g_vol_max, g_vol_min / 256.0, g_vol_max / 256.0, cur_vol);
        if (volume_set_pct(g_vol_pct) == 0)
            fprintf(stderr, "[音量] 已设为 %d%%\n", g_vol_pct);
    } else {
        fprintf(stderr, "[音量] 设备未提供硬件音量控制（%s），请用软件音量\n", strerror(-r));
    }
    if (ctl_path) {
        g_ctl_fd = open(ctl_path, O_RDWR | O_NONBLOCK);
        if (g_ctl_fd < 0)
            fprintf(stderr, "[音量] 打开控制管道 %s 失败: %s（音量改动将不生效）\n",
                    ctl_path, strerror(errno));
        else
            fprintf(stderr, "[音量] 控制管道就绪：%s\n", ctl_path);
    }

    ring_cap = (size_t)((double)g_rate * g_ch * g_bytes * buf_sec);
    if (ring_cap < 4096) ring_cap = 4096;
    ring = calloc(1, ring_cap);
    slots = calloc((size_t)g_nurbs, sizeof(urb_slot_t));
    if (!ring || !slots) { fprintf(stderr, "内存不足\n"); release_iface(); return 2; }
    int pkt_max = (int)(((double)g_rate / 1000.0 + 1.0) * g_ch * g_bytes);
    for (int i = 0; i < g_nurbs; i++) {
        slots[i].urb = calloc(1, sizeof(struct usbdevfs_urb) +
                                 sizeof(struct usbdevfs_iso_packet_desc) * (size_t)g_fs);
        slots[i].buf = malloc((size_t)pkt_max * (size_t)g_fs + 64);
        if (!slots[i].urb || !slots[i].buf) { fprintf(stderr, "内存不足\n"); release_iface(); return 2; }
        struct usbdevfs_urb *u = slots[i].urb;
        u->type = USBDEVFS_URB_TYPE_ISO;
        u->endpoint = (uint8_t)g_ep_out;
        u->number_of_packets = g_fs;
        u->usercontext = &slots[i];
    }

    pthread_t th;
    if (pthread_create(&th, NULL, feeder, NULL) != 0) {
        fprintf(stderr, "线程创建失败\n"); release_iface(); return 2;
    }

    int inflight = 0;
    /* 先攒一点数据再开闸，避免起播第一包补静音 */
    for (int i = 0; i < 2000; i++) {
        size_t need = ring_cap / 4;
        int enough;
        pthread_mutex_lock(&ring_mx);
        enough = (ring_cnt >= need) || g_eof || g_quit;
        pthread_mutex_unlock(&ring_mx);
        if (enough) break;
        usleep(1000);
    }
    g_t0 = now_sec();
    g_submitted = 0;
    for (int i = 0; i < g_nurbs && !g_quit; i++) {
        int npk = fill_and_submit(&slots[i]);
        if (npk < 0) {
            fprintf(stderr, "[错误] 提交 URB 失败: %s\n", strerror(errno));
            release_iface();
            return 5;
        }
        if (npk > 0) { slots[i].busy = 1; inflight++; }
    }

    struct timespec t0;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    double last_stat = 0.0;
    unsigned long iso_errors = 0;
    int stopping = 0;

    /* ==== 主循环：先收完成，再按 1ms 节拍补发，空转时小睡 ==== */
    while (!g_quit) {
        if (g_paused) {
            while (inflight > 0) {
                struct usbdevfs_urb *done = NULL;
                if (ioctl(g_fd, USBDEVFS_REAPURBNDELAY, &done) < 0 || !done) break;
                inflight--;
            }
            set_alt(0);
            while (g_paused && !g_quit) {
                ctl_pump();
                if (!watch_alive()) { g_quit = 1; break; }
                usleep(20000);
            }
            if (g_quit) break;
            set_alt(g_alt);
            g_t0 = now_sec();          /* 恢复后重新对时 */
            g_submitted = 0;
            g_next_frame = -1;         /* 帧号重新问内核 */
            continue;
        }

        ctl_pump();
        if (!watch_alive()) { g_quit = 1; break; }

        /* 1) 收已完成的 URB */
        int did_something = 0;
        for (;;) {
            struct usbdevfs_urb *done = NULL;
            if (ioctl(g_fd, USBDEVFS_REAPURBNDELAY, &done) < 0 || !done) break;
            inflight--;
            did_something = 1;
            if (done->usercontext) ((urb_slot_t *)done->usercontext)->busy = 0;
            if (g_next_frame < 0 && done->number_of_packets > 0)
                g_next_frame = (done->start_frame + done->number_of_packets) & 0x7FF;
            if (done->status < 0 || done->error_count > 0) {
                iso_errors++;
                if (iso_errors <= 5)
                    fprintf(stderr, "[警告] URB 完成异常 status=%d error_count=%d\n",
                            done->status, done->error_count);
            }
        }

        /* 2) 按节拍补发：只要还有空槽、并且到了该发的包，就继续发 */
        if (!stopping) {
            if (g_eof && ring_empty()) {
                stopping = 1;
            } else {
                for (int i = 0; i < g_nurbs && !g_quit; i++) {
                    if (slots[i].busy) continue;
                    int npk = fill_and_submit(&slots[i]);
                    if (npk < 0) { stopping = 1; break; }
                    if (npk > 0) {
                        slots[i].busy = 1;
                        inflight++;
                        did_something = 1;
                    }
                }
            }
        } else if (inflight <= 0) {
            break;                      /* 收尾：发完剩下的就退出 */
        }

        /* 3) 没事可做就小睡：粒度要细，否则补包会晚到造成断续 */
        if (!did_something) usleep(200);

        /* 4) 统计与时限 */
        struct timespec now_ts;
        clock_gettime(CLOCK_MONOTONIC, &now_ts);
        double el = (now_ts.tv_sec - t0.tv_sec) + (now_ts.tv_nsec - t0.tv_nsec) / 1e9;
        if (el - last_stat >= 1.0) {
            last_stat = el;
            fprintf(stderr, "[状态] %.1fs 已送 %.1f KB / 欠载包 %lu / 异常 %lu / 在途 %d / 帧错 %d\n",
                    el, g_bytes_sent / 1024.0, g_underrun_packets, iso_errors,
                    inflight, g_frame_errors);
        }
        if (seconds > 0 && el >= seconds) break;
    }

    if (g_quit || stopping) {
        set_alt(0);
    }
    pthread_mutex_lock(&ring_mx);
    g_quit = 1;
    pthread_cond_broadcast(&ring_cv);
    pthread_mutex_unlock(&ring_mx);
    pthread_join(th, NULL);

    fprintf(stderr, "[结束] 共送 %.1f KB，欠载 %lu 包，异常 %lu 次\n",
            g_bytes_sent / 1024.0, g_underrun_packets, iso_errors);

    release_iface();     /* alt 0 -> release -> 重新绑定内核驱动 */
    return 0;
}
