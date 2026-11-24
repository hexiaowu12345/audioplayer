import os
import json
import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYLISTS_FILE = os.path.join(_ROOT, 'playlists.json')

# 内置歌单名，不允许用户创建/重命名成这些名字
RESERVED_NAMES = {'默认', 'default'}


def load_all():
    """读取全部自定义歌单，返回 dict。"""
    if not os.path.exists(PLAYLISTS_FILE):
        return {}
    try:
        with open(PLAYLISTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_all(data):
    try:
        with open(PLAYLISTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return True
    except Exception:
        return False


def names():
    """按名称排序返回所有歌单名。"""
    return sorted(load_all().keys())


def clean_name(name):
    return str(name or '').strip()


def find_existing(name):
    """忽略首尾空格和大小写，返回真实存在的歌单名；不存在返回 None。"""
    key = clean_name(name).casefold()
    if not key:
        return None
    for n in load_all():
        if n.strip().casefold() == key:
            return n
    return None


def exists(name):
    return find_existing(name) is not None


def similar(name):
    """找相近的歌单名，仅用于提示。"""
    import difflib
    try:
        m = difflib.get_close_matches(clean_name(name), names(), n=1, cutoff=0.6)
    except Exception:
        m = []
    return m[0] if m else None


def get_tracks(name):
    """返回歌单内仍存在的曲目（自动清理已删除的文件）。"""
    data = load_all()
    real = find_existing(name)
    pl = data.get(real) if real else None
    if not pl:
        return []
    tracks = pl.get('tracks', [])
    alive = [t for t in tracks if isinstance(t, str) and os.path.exists(t)]
    if len(alive) != len(tracks):
        pl['tracks'] = alive
        save_all(data)
    return alive


def create(name):
    name = clean_name(name)
    if not name:
        return False
    if name.casefold() in {r.casefold() for r in RESERVED_NAMES}:
        return False
    if find_existing(name):
        return False
    data = load_all()
    data[name] = {
        'created': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'tracks': [],
    }
    return save_all(data)


def delete(name):
    data = load_all()
    real = find_existing(name)
    if not real:
        return False
    del data[real]
    return save_all(data)


def rename(old, new):
    data = load_all()
    real = find_existing(old)
    new = clean_name(new)
    if not real or not new:
        return False
    if new.casefold() in {r.casefold() for r in RESERVED_NAMES}:
        return False
    if find_existing(new):
        return False
    data[new] = data.pop(real)
    return save_all(data)


def add_tracks(name, paths):
    """把曲目加进歌单（自动去重），返回实际新增数量。"""
    data = load_all()
    real = find_existing(name)
    pl = data.get(real) if real else None
    if not pl:
        return -1
    tracks = pl.setdefault('tracks', [])
    added = 0
    for p in paths:
        if p and os.path.exists(p) and p not in tracks:
            tracks.append(p)
            added += 1
    if added:
        save_all(data)
    return added


def move_track(name, index, target=None, delta=None):
    """把歌单内第 index 首（1-based）挪到 target 位置，或相对挪 delta 位。

    target 支持负数：-1 表示最后一首，-2 倒数第二。
    返回 (新位置, 曲目路径)；失败返回 (None, None)。
    """
    data = load_all()
    real = find_existing(name)
    pl = data.get(real) if real else None
    if not pl:
        return None, None

    tracks = pl.get('tracks', [])
    n = len(tracks)
    if not (1 <= index <= n):
        return None, None

    if target is not None:
        pos = n + 1 + target if target < 0 else target
    elif delta is not None:
        pos = index + delta
    else:
        return None, None

    pos = max(1, min(n, pos))
    if pos == index:
        return index, tracks[index - 1]

    item = tracks.pop(index - 1)
    tracks.insert(pos - 1, item)
    pl['tracks'] = tracks
    save_all(data)
    return pos, item


def remove_tracks(name, indexes):
    """按歌单内序号(1-based)移除曲目，返回 (移除数量, 被移除路径列表)。"""
    data = load_all()
    real = find_existing(name)
    pl = data.get(real) if real else None
    if not pl:
        return 0, []
    tracks = pl.get('tracks', [])
    picked = []
    for i in indexes:
        if 1 <= i <= len(tracks):
            picked.append(i - 1)
    removed_paths = []
    for i in sorted(set(picked), reverse=True):
        removed_paths.append(tracks.pop(i))
    if picked:
        save_all(data)
    return len(removed_paths), list(reversed(removed_paths))
