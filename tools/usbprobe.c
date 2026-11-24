/* usbprobe.c - USB 音频设备拓扑探针（Termux / 无 root / usbfs fd）
 *
 * 三种用法：
 *   termux-usb -r -E -e ./usbprobe /dev/bus/usb/001/002 json
 *        连上设备，输出一行 JSON 拓扑（播放器靠这个自动适配小尾巴）
 *   termux-usb -r -E -e ./usbprobe /dev/bus/usb/001/002        （默认 human）
 *        人看的详细描述符 dump
 *   ./usbprobe parse-hex < dump.txt
 *        离线解析：吃十六进制配置描述符文本，输出同样的 JSON（不需要设备）
 *
 * 输出的 JSON 字段：
 *   vid pid manufacturer product uac2
 *   ac_iface as_iface ep_out clock_id unit_id has_volume capture_iface
 *   alts: [{alt, bytes, bits, mps, interval}]     16/24/32bit 各自对应哪个 alt
 *
 * 只做读取，不 claim 接口、不改设备状态。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <libusb-1.0/libusb.h>

#define MAX_ALT 8

typedef struct {
    int alt, bytes, bits, mps, interval;
} alt_t;

typedef struct {
    int vid, pid;
    char manufacturer[128], product[128];
    int uac2, ac_iface, as_iface, ep_out, clock_id, unit_id, has_volume, capture_iface;
    alt_t alts[MAX_ALT];
    int n_alts;
} profile_t;

static void profile_init(profile_t *p)
{
    memset(p, 0, sizeof *p);
    p->vid = -1; p->pid = -1;
    p->ac_iface = -1; p->as_iface = -1; p->ep_out = -1;
    p->clock_id = -1; p->unit_id = -1; p->capture_iface = -1;
    p->has_volume = 0; p->uac2 = 0; p->n_alts = 0;
}

static void add_alt(profile_t *p, int alt)
{
    if (p->n_alts >= MAX_ALT) return;
    for (int i = 0; i < p->n_alts; i++)
        if (p->alts[i].alt == alt) { p->alts[i].mps = 0; p->alts[i].bits = 0; return; }
    p->alts[p->n_alts].alt = alt;
    p->alts[p->n_alts].bytes = 0;
    p->alts[p->n_alts].bits = 0;
    p->alts[p->n_alts].mps = 0;
    p->alts[p->n_alts].interval = 0;
    p->n_alts++;
}

static alt_t *cur_alt_ref(profile_t *p, int alt)
{
    for (int i = 0; i < p->n_alts; i++)
        if (p->alts[i].alt == alt) return &p->alts[i];
    return NULL;
}

/* ---------- 描述符解析（UAC1 / UAC2 都认） ---------- */
static void parse_config(const unsigned char *cfg, int len, profile_t *p)
{
    int i = 0, cur_if = -1, cur_alt = 0, cur_is_as_play = 0, subslot = 0, bits = 0;

    while (i + 2 <= len) {
        int bl = cfg[i], bt = cfg[i + 1];
        if (bl < 2 || i + bl > len) break;
        const unsigned char *d = cfg + i;

        if (bt == 0x04 && bl >= 9) {                 /* INTERFACE */
            cur_if  = d[2];
            cur_alt = d[3];
            cur_is_as_play = 0;
            subslot = 0; bits = 0;
            int cls = d[5], sub = d[6], proto = d[7];
            if (cls == 0x01 && sub == 0x01) {
                if (p->ac_iface < 0) p->ac_iface = cur_if;
                if (proto == 0x20) p->uac2 = 1;
            } else if (cls == 0x01 && sub == 0x02) {
                cur_is_as_play = 1;
            }
        } else if (bt == 0x24 && bl >= 4) {          /* CS_INTERFACE */
            int st = d[2];
            if (st == 0x01 && cur_if == p->ac_iface && bl >= 9) {
                int adc = d[3] | (d[4] << 8);
                if (adc >= 0x0200) p->uac2 = 1;
            } else if (st == 0x0A && cur_if == p->ac_iface && bl >= 5) {
                if (p->clock_id < 0) p->clock_id = d[3];     /* CLOCK_SOURCE */
            } else if (st == 0x06 && cur_if == p->ac_iface && bl >= 6) {
                if (p->unit_id < 0) {
                    p->unit_id = d[3];                        /* FEATURE_UNIT */
                    unsigned int c0 = d[5] | (d[6] << 8) | (d[7] << 16);
                    p->has_volume = (c0 & 0x02) ? 1 : 0;      /* D1 = Volume */
                }
            } else if (st == 0x02 && cur_is_as_play) {
                if (p->uac2 && bl >= 6) {                     /* UAC2 FORMAT_TYPE_I */
                    subslot = d[4]; bits = d[5];
                } else if (!p->uac2 && bl >= 8) {              /* UAC1 FORMAT_TYPE_I */
                    subslot = d[5]; bits = d[6];
                }
            }
        } else if (bt == 0x05 && bl >= 7) {          /* ENDPOINT */
            int ep = d[2], attr = d[3];
            int mps = d[4] | (d[5] << 8);
            if ((attr & 0x03) == 0x01) {                       /* 等时端点 */
                if (!(ep & 0x80) && cur_is_as_play) {
                    if (p->as_iface < 0) p->as_iface = cur_if;
                    if (p->ep_out < 0) p->ep_out = ep;
                    add_alt(p, cur_alt);
                    alt_t *a = cur_alt_ref(p, cur_alt);
                    if (a) {
                        a->bytes = subslot ? subslot : (bits / 8);
                        a->bits = bits;
                        a->mps = mps;
                        a->interval = d[6];
                    }
                } else if (ep & 0x80) {
                    if (p->capture_iface < 0) p->capture_iface = cur_if;
                }
            }
        }
        i += bl;
    }
}


/* ---------- 从 libusb 已解析的结构里读拓扑 ----------
 * 不用再发原始 GET_DESCRIPTOR：Android 的 usbfs 对自定义长度的控制传输
 * 有时只回 9 字节（只给配置头），结果解析出一份"空档案"，
 * 之前就是因此把设备误判成 UAC1、时钟源 -1、无音量，进而把系统音频搞哑。
 * libusb 的 interface/endpoint 结构里都带着 class-specific 的 extra 字节，
 * 直接遍历它们最稳。
 */
static void scan_extra(const unsigned char *extra, int len, int is_ac, profile_t *p)
{
    int i = 0;
    while (extra && i + 3 <= len) {
        int bl = extra[i];
        if (bl < 3 || i + bl > len) break;
        if (extra[i + 1] == 0x24) {                  /* CS_INTERFACE */
            int st = extra[i + 2];
            if (is_ac && st == 0x01 && bl >= 5) {
                int adc = extra[i + 3] | (extra[i + 4] << 8);
                if (adc >= 0x0200) p->uac2 = 1;
            } else if (is_ac && st == 0x0A && bl >= 5) {
                if (p->clock_id < 0) p->clock_id = extra[i + 3];
            } else if (is_ac && st == 0x06 && bl >= 8) {
                if (p->unit_id < 0) {
                    p->unit_id = extra[i + 3];
                    unsigned int c0 = extra[i + 5] | (extra[i + 6] << 8) | (extra[i + 7] << 16);
                    p->has_volume = (c0 & 0x02) ? 1 : 0;
                }
            }
        }
        i += bl;
    }
}

/* 在 AS 接口的 extra 里找 FORMAT_TYPE_I，拿子帧字节数和位深 */
static void scan_as_format(const unsigned char *extra, int len, int uac2,
                           int *subslot, int *bits)
{
    int i = 0;
    *subslot = 0; *bits = 0;
    while (extra && i + 3 <= len) {
        int bl = extra[i];
        if (bl < 3 || i + bl > len) break;
        if (extra[i + 1] == 0x24 && extra[i + 2] == 0x02) {   /* FORMAT_TYPE */
            if (uac2 && bl >= 6) { *subslot = extra[i + 4]; *bits = extra[i + 5]; }
            else if (!uac2 && bl >= 7) { *subslot = extra[i + 5]; *bits = extra[i + 6]; }
        }
        i += bl;
    }
}

static void parse_from_libusb(struct libusb_config_descriptor *cd, profile_t *p)
{
    for (int i = 0; i < cd->bNumInterfaces; i++) {
        const struct libusb_interface *it = &cd->interface[i];
        for (int a = 0; a < it->num_altsetting; a++) {
            const struct libusb_interface_descriptor *id = &it->altsetting[a];
            int cls = id->bInterfaceClass, sub = id->bInterfaceSubClass;
            int proto = id->bInterfaceProtocol;

            if (cls == 0x01 && sub == 0x01) {                 /* 音频控制接口 */
                if (p->ac_iface < 0) p->ac_iface = id->bInterfaceNumber;
                if (proto == 0x20) p->uac2 = 1;
                scan_extra(id->extra, id->extra_length, 1, p);
            } else if (cls == 0x01 && sub == 0x02) {          /* 音频流接口 */
                int subslot = 0, bits = 0;
                scan_as_format(id->extra, id->extra_length, p->uac2, &subslot, &bits);
                for (int e = 0; e < id->bNumEndpoints; e++) {
                    const struct libusb_endpoint_descriptor *ep = &id->endpoint[e];
                    int is_iso = (ep->bmAttributes & 0x03) == 0x01;
                    if (!is_iso) continue;
                    if (ep->bEndpointAddress & 0x80) {        /* IN = 录音 */
                        if (p->capture_iface < 0) p->capture_iface = id->bInterfaceNumber;
                    } else {
                        if (p->as_iface < 0) p->as_iface = id->bInterfaceNumber;
                        if (p->ep_out < 0) p->ep_out = ep->bEndpointAddress;
                        add_alt(p, id->bAlternateSetting);
                        alt_t *at = cur_alt_ref(p, id->bAlternateSetting);
                        if (at && ep->wMaxPacketSize > at->mps) {
                            at->bytes = subslot ? subslot : (bits / 8);
                            at->bits = bits;
                            at->mps = ep->wMaxPacketSize;
                            at->interval = ep->bInterval;
                        }
                    }
                }
            }
        }
    }
}

/* ---------- 输出 ---------- */
static void print_json(const profile_t *p)
{
    printf("{\"vid\":\"%04x\",\"pid\":\"%04x\",", p->vid, p->pid);
    printf("\"manufacturer\":\"%s\",\"product\":\"%s\",",
           p->manufacturer, p->product);
    printf("\"uac2\":%s,\"ac_iface\":%d,\"as_iface\":%d,\"ep_out\":%d,",
           p->uac2 ? "true" : "false", p->ac_iface, p->as_iface, p->ep_out);
    printf("\"clock_id\":%d,\"unit_id\":%d,\"has_volume\":%s,\"capture_iface\":%d,",
           p->clock_id, p->unit_id, p->has_volume ? "true" : "false", p->capture_iface);
    printf("\"alts\":[");
    for (int i = 0; i < p->n_alts; i++) {
        const alt_t *a = &p->alts[i];
        if (i) printf(",");
        printf("{\"alt\":%d,\"bytes\":%d,\"bits\":%d,\"mps\":%d,\"interval\":%d}",
               a->alt, a->bytes, a->bits, a->mps, a->interval);
    }
    printf("]}\n");
}

static void print_human(const profile_t *p)
{
    printf("=== USB 音频拓扑 ===\n");
    printf("VID:PID        : %04x:%04x\n", p->vid, p->pid);
    printf("厂商 / 产品    : %s / %s\n", p->manufacturer, p->product);
    printf("协议版本       : %s\n", p->uac2 ? "UAC2" : "UAC1");
    printf("音频控制接口   : %d\n", p->ac_iface);
    printf("播放流接口     : %d   端点 0x%02x\n", p->as_iface, p->ep_out);
    printf("时钟源 ID      : %d\n", p->clock_id);
    printf("Feature Unit   : %d   %s\n", p->unit_id,
           p->has_volume ? "支持音量控制" : "无音量控制");
    printf("录音流接口     : %d\n", p->capture_iface);
    printf("播放 alt 表:\n");
    for (int i = 0; i < p->n_alts; i++) {
        const alt_t *a = &p->alts[i];
        printf("  alt %d : %2d bit / 子帧 %d 字节 / 单包上限 %d 字节 / 间隔 %d ms\n",
               a->alt, a->bits, a->bytes, a->mps, a->interval ? a->interval : 1);
    }
    printf("=== 结束 ===\n");
}

/* 从文本里读十六进制字节（离线解析用） */
static int read_hex_stream(unsigned char *buf, int cap)
{
    int n = 0, hi = -1;
    int c;
    while ((c = getchar()) != EOF) {
        int v;
        if (c >= '0' && c <= '9') v = c - '0';
        else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v = c - 'A' + 10;
        else { hi = -1; continue; }
        if (hi < 0) { hi = v; continue; }
        if (n < cap) buf[n++] = (unsigned char)((hi << 4) | v);
        hi = -1;
    }
    return n;
}

static int do_hex_mode(void)
{
    static unsigned char buf[8192];
    int n = read_hex_stream(buf, (int)sizeof buf);
    if (n < 9) { fprintf(stderr, "十六进制数据太少（%d 字节）\n", n); return 2; }
    profile_t p;
    profile_init(&p);
    strcpy(p.manufacturer, "hex");
    strcpy(p.product, "hex");
    parse_config(buf, n, &p);
    print_json(&p);
    return 0;
}

int main(int argc, char **argv)
{
    /* 模式优先取命令行；命令行没给就读 USBPROBE_MODE
       —— termux-usb 的 -e 只能接一条命令，额外参数并不可靠 */
    const char *mode = (argc > 1) ? argv[1] : "";
    if (!mode || !*mode) {
        const char *m = getenv("USBPROBE_MODE");
        if (m && *m) mode = m;
    }

    if (strcmp(mode, "parse-hex") == 0) return do_hex_mode();

    int fd = -1;
    const char *e = getenv("TERMUX_USB_FD");
    if (e && *e) fd = atoi(e);
    if (fd < 0) {
        fprintf(stderr, "用法: termux-usb -r -E -e %s <设备> [json|human]\n", argv[0]);
        fprintf(stderr, "      %s parse-hex < 十六进制dump.txt\n", argv[0]);
        return 1;
    }

    libusb_context *ctx = NULL;
    if (libusb_init(&ctx) < 0) { fprintf(stderr, "libusb_init 失败\n"); return 2; }
    libusb_device_handle *h = NULL;
    int r = libusb_wrap_sys_device(ctx, (intptr_t)fd, &h);
    if (r < 0) {
        fprintf(stderr, "libusb_wrap_sys_device 失败: %s\n", libusb_error_name(r));
        return 3;
    }
    libusb_device *dev = libusb_get_device(h);
    struct libusb_device_descriptor dd;
    libusb_get_device_descriptor(dev, &dd);

    profile_t p;
    profile_init(&p);
    p.vid = dd.idVendor;
    p.pid = dd.idProduct;
    unsigned char s[256];
    if (libusb_get_string_descriptor_ascii(h, dd.iManufacturer, s, sizeof s) > 0)
        snprintf(p.manufacturer, sizeof p.manufacturer, "%s", (char *)s);
    if (libusb_get_string_descriptor_ascii(h, dd.iProduct, s, sizeof s) > 0)
        snprintf(p.product, sizeof p.product, "%s", (char *)s);

    struct libusb_config_descriptor *cd = NULL;
    if (libusb_get_active_config_descriptor(dev, &cd) != 0 &&
        libusb_get_config_descriptor(dev, 0, &cd) != 0) {
        fprintf(stderr, "读不到配置描述符\n");
        return 4;
    }
    parse_from_libusb(cd, &p);
    libusb_free_config_descriptor(cd);

    /* 完整性校验：拿不到播放流接口 / 端点 / 位深表就宁可报错，绝不缓存半份档案 */
    if (p.as_iface < 0 || p.ep_out < 0 || p.n_alts == 0) {
        fprintf(stderr, "[错误] 拓扑不完整：as_iface=%d ep=%d alts=%d\n",
                p.as_iface, p.ep_out, p.n_alts);
        return 5;
    }

    if (strcmp(mode, "json") == 0) print_json(&p);
    else { print_human(&p); }

    libusb_close(h);
    libusb_exit(ctx);
    return 0;
}
