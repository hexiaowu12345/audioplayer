import json
import os

_STRINGS = {}

def _load_strings():
    global _STRINGS
    path = os.path.join(os.path.dirname(__file__), 'messages.json')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            _STRINGS = json.load(f)

_load_strings()

def get_str(key, **kwargs):
    """获取文案，支持占位符格式化"""
    s = _STRINGS.get(key, key)
    if kwargs:
        try:
            return s.format(**kwargs)
        except KeyError:
            pass
    return s