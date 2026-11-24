import sys
import os
from core.engine import start_playback

def main():
    if len(sys.argv) > 1:
        target_folders = sys.argv[1:]
    else:
        # 根据操作系统自动选择默认路径
        if sys.platform.startswith('win'):
            target_folders = [
                r"C:\music",
                # 以后要加新路径就往这里加
            ]
        elif ('com.termux' in os.environ.get('PREFIX', '')
              or sys.platform.startswith('android')):
            # Termux / Android
            target_folders = [
                "/sdcard/下载",
                "/sdcard/qqmusic",
                "/sdcard/我的文件"
            ]
        else:
            # 桌面 Linux（macOS 也能跑，目录不存在会自动跳过）
            home = os.path.expanduser('~')
            target_folders = [
                os.path.join(home, 'Music'),
                os.path.join(home, '音乐'),
                os.path.join(home, 'Downloads'),
            ]

    # 确保所有功能模块已加载并注册命令
    import module

    start_playback(target_folders)

if __name__ == "__main__":
    main()