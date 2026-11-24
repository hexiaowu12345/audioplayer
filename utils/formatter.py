def format_time(seconds):
    """把秒数格式化成 mm:ss 或 h:mm:ss"""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "00:00"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"