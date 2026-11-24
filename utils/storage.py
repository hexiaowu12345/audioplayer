import os
import json

PROGRESS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'progress.json')
SCAN_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'scan_cache.json')

def _read_progress_raw():
    """读取 progress.json 全文（保留 file/position/settings 等所有键）。"""
    try:
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_progress_raw(data):
    try:
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return True
    except Exception:
        return False


def save_progress(file_path, position):
    """保存当前播放的文件和位置（不覆盖 settings 等其他字段）"""
    data = _read_progress_raw()
    data["file"] = file_path
    data["position"] = position
    _write_progress_raw(data)


# ================= 新增：播放器设置（存在 progress.json 的 settings 里） =================
def load_settings():
    """读取 settings 段（USB 独占 / 极致完美输出 / 音量等）。"""
    data = _read_progress_raw()
    s = data.get("settings")
    return dict(s) if isinstance(s, dict) else {}


def save_settings(patch):
    """合并写入 settings 段，其余字段（file/position）原样保留。"""
    if not isinstance(patch, dict):
        return False
    data = _read_progress_raw()
    s = dict(data.get("settings") or {})
    s.update(patch)
    data["settings"] = s
    return _write_progress_raw(data)

def load_progress():
    """读取上次播放的记录，返回 (文件路径, 位置秒数)"""
    if not os.path.exists(PROGRESS_FILE):
        return None, 0.0
    try:
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('file'), float(data.get('position', 0.0))
    except Exception:
        return None, 0.0

def clear_progress():
    """清除进度记录"""
    if os.path.exists(PROGRESS_FILE):
        try:
            os.remove(PROGRESS_FILE)
        except Exception:
            pass

# ================= 新增：扫描缓存功能 =================
def save_scan_cache(files):
    """保存扫描到的音乐列表到外部文件"""
    try:
        with open(SCAN_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(files, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

def load_scan_cache():
    """读取外部缓存文件，返回音乐列表；若无缓存则返回 None"""
    if not os.path.exists(SCAN_CACHE_FILE):
        return None
    try:
        with open(SCAN_CACHE_FILE, 'r', encoding='utf-8') as f:
            files = json.load(f)
            if isinstance(files, list) and len(files) > 0:
                return files
            return None
    except Exception:
        return None

def clear_scan_cache():
    """清除扫描缓存"""
    if os.path.exists(SCAN_CACHE_FILE):
        try:
            os.remove(SCAN_CACHE_FILE)
        except Exception:
            pass