#!/usr/bin/env python3
"""跨平台兼容自检脚本（Windows / 桌面 Linux / Termux 都能跑）

用法：
    python tools/compat_check.py
（源码放在 /sdcard 上不能直接执行，用 python 调用即可）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import module  # noqa: F401  确保命令模块都能导入
except Exception as exc:
    print('模块导入失败：%r' % exc)
    raise SystemExit(2)

from utils.compat import report

if __name__ == '__main__':
    raise SystemExit(report())
