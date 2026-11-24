import os
import sys
import json
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_ROOT, 'usb_audio.json')


def load_config():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return True
    except Exception:
        return False


def get_forced_device():
    """返回独占输出设置：{'device': 'hw:1,0'}；未启用返回 None。"""
    cfg = load_config()
    if cfg.get('enabled') and cfg.get('device'):
        return cfg
    return None


def _read(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception:
        return None


def detect_cards():
    """解析 /proc/asound/cards。

    返回 (cards, error)。cards 是 [{index, id, name, usb, rates}]
    """
    text = _read('/proc/asound/cards')
    if text is None:
        return [], '无法读取 /proc/asound/cards（需要 root 或系统限制）'

    cards = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        line = line.rstrip()
        if not line.strip():
            continue
        if '[' in line and ']' in line and ':' in line:
            head = line.split(':', 1)[0]
            idx_part = head.split('[', 1)[0].strip()
            if not idx_part.isdigit():
                continue
            cid = line.split('[', 1)[1].split(']', 1)[0].strip()
            rest = line.split(']', 1)[1]
            driver = rest.split(':', 1)[0].strip() if ':' in rest else ''
            name = rest.split(':', 1)[1].strip() if ':' in rest else rest.strip()
            desc = lines[i + 1].strip() if i + 1 < len(lines) else ''
            usb = ('usb' in (driver + name + desc).lower())
            cards.append({
                'index': int(idx_part), 'id': cid, 'driver': driver,
                'name': name, 'desc': desc, 'usb': usb, 'rates': [],
            })

    for card in cards:
        stream = _read(f"/proc/asound/card{card['index']}/stream0")
        if not stream:
            continue
        rates = []
        for row in stream.splitlines():
            row = row.strip()
            if row.lower().startswith('rates:'):
                rates = [x.strip() for x in row.split(':', 1)[1].split(',') if x.strip()]
        card['rates'] = rates
    return cards, None


def list_usb_cards():
    cards, err = detect_cards()
    return [c for c in cards if c.get('usb')], err, cards


def is_root():
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def device_hint(card):
    """给 mpv/ffplay 用的 ALSA 设备名。"""
    return f"hw:{card['index']},0"


def summary_line():
    """给 info 展示用的一行输出设备摘要。"""
    if not sys.platform.startswith('linux'):
        return '系统默认输出'
    usb_cards, err, cards = list_usb_cards()
    if usb_cards:
        c = usb_cards[0]
        return f"USB Audio: {c['name']}" + (f" ({', '.join(c['rates'])} Hz)" if c['rates'] else '')
    if err:
        return '内置输出（声卡信息不可读，需 root）'
    return '内置输出（未检测到 USB 声卡）'


def apply_to_env(env):
    """按独占设置返回播放环境变量副本。"""
    env2 = dict(env)
    forced = get_forced_device()
    if forced:
        env2['SDL_AUDIODRIVER'] = 'alsa'
    return env2


def extra_player_args():
    """返回 mpv 需要的额外参数列表。"""
    forced = get_forced_device()
    if not forced:
        return []
    return [f"--audio-device=alsa/{forced['device']}"]
