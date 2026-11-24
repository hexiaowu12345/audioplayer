import os
import json
import subprocess
from strings import get_str
from utils.formatter import format_time
from .config import FFPROBE_PATH, AUDIO_EXTENSIONS


def get_audio_files(root_dir, recursive=True):
    """扫描目录下支持的音频文件。

    recursive=True  递归整棵子树（默认，保持旧行为）
    recursive=False 只看当前这一层
    """
    audio_files = []
    if not recursive:
        try:
            for f in os.listdir(root_dir):
                full = os.path.join(root_dir, f)
                if os.path.isfile(full) and os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS:
                    audio_files.append(full)
        except OSError:
            pass
        return audio_files

    for dirpath, _, filenames in os.walk(root_dir):
        for f in filenames:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS:
                audio_files.append(os.path.join(dirpath, f))
    return audio_files


# ==================== 元数据读取：mutagen 优先，ffprobe 兜底 ====================
try:
    from mutagen import File as _MutagenFile
    HAS_MUTAGEN = True
except Exception:
    _MutagenFile = None
    HAS_MUTAGEN = False

LOSSY_CODECS = {'MP3', 'AAC', 'VORBIS', 'OGGVORBIS', 'OPUS', 'WMA', 'MP3FLOAT'}

_CODEC_BY_CLASS = {
    'MP3': 'MP3', 'MP2': 'MP2', 'FLAC': 'FLAC',
    'OggVorbis': 'VORBIS', 'OggOpus': 'OPUS', 'OggSpeex': 'SPEEX',
    'WAVE': 'PCM', 'AIFF': 'PCM', 'ASF': 'WMA',
    'MonkeysAudio': 'APE', 'WavPack': 'WAVPACK', 'TrueAudio': 'TTA',
}

_FORMAT_LABELS = {
    '.mp3': 'MP2/3 (MPEG audio layer 2/3)',
    '.flac': 'FLAC (Free Lossless Audio Codec)',
    '.ogg': 'Ogg', '.opus': 'Ogg Opus',
    '.m4a': 'MPEG-4', '.mp4': 'MPEG-4',
    '.aac': 'AAC (Advanced Audio Coding)',
    '.wav': 'WAV (Waveform Audio)',
    '.wma': 'ASF (Windows Media Audio)',
}


def _tags_dict(tags):
    """把 mutagen 标签统一成小写 key 的 dict。"""
    out = {}
    if not tags:
        return out
    try:
        items = list(tags.items())
    except Exception:
        items = []
    for k, v in items:
        key = str(k).lower()
        if isinstance(v, (list, tuple)):
            v = ' / '.join(str(x) for x in v)
        out[key] = str(v)
    return out


def _pick(tags, *keys):
    for k in keys:
        v = tags.get(k)
        if v:
            return v
    return ''


def _has_cover(audio):
    tags = getattr(audio, 'tags', None)
    if not tags:
        return False
    try:
        keys = [str(k).lower() for k in tags.keys()]
    except Exception:
        return False
    return any(k.startswith('apic') or k in ('covr', 'metadata_block_picture') for k in keys)


def _mutagen_metadata(file_path):
    """用 mutagen 直接解析文件头，不启动子进程。

    信息不全（如个别 wav）时返回 None，交给 ffprobe 兜底。
    """
    if not HAS_MUTAGEN:
        return None
    try:
        audio = _MutagenFile(file_path)
        if audio is None or getattr(audio, 'info', None) is None:
            return None

        info = audio.info
        duration = float(getattr(info, 'length', 0) or 0)
        sample_rate = int(getattr(info, 'sample_rate', 0) or 0)
        channels = int(getattr(info, 'channels', 0) or 0)
        if duration <= 0 or sample_rate <= 0:
            return None

        class_name = type(audio).__name__
        codec = _CODEC_BY_CLASS.get(class_name)
        if class_name == 'MP4':
            c = str(getattr(info, 'codec', '') or '').lower()
            if 'alac' in c:
                codec = 'ALAC'
            elif 'mp4a' in c or 'aac' in c:
                codec = 'AAC'
            else:
                codec = 'MP4'
        codec = codec or class_name.upper()

        tags = _tags_dict(getattr(audio, 'tags', None))
        title = _pick(tags, 'title', 'tit2', '©nam') or os.path.basename(file_path)
        artist = _pick(tags, 'artist', 'tpe1', '©art', 'albumartist', 'tpe2') or '未知艺术家'

        file_size = os.path.getsize(file_path)
        bit_rate = int(getattr(info, 'bitrate', 0) or 0)
        bit_rate_estimated = False
        if bit_rate <= 0 and duration > 0 and file_size > 0:
            bit_rate = int(file_size * 8 / duration)
            bit_rate_estimated = True

        # ---- 位深 / 解码格式（mutagen 不提供 sample_fmt，按位深推断）----
        bits_raw = int(getattr(info, 'bits_per_sample', 0) or 0)
        if codec in LOSSY_CODECS:
            bits = str(bits_raw) if bits_raw else '16'
            sample_fmt = 'fltp'
        else:
            bits = str(bits_raw) if bits_raw else '未知'
            sample_fmt = {16: 's16', 24: 's24', 32: 's32'}.get(bits_raw, '')

        kbps = (f"≈{bit_rate // 1000} kbps" if bit_rate_estimated
                else f"{bit_rate // 1000} kbps") if bit_rate > 0 else "未知码率"
        khz = f"{sample_rate / 1000:g} kHz"

        ext = os.path.splitext(file_path)[1].lower()
        return {
            'title': title, 'artist': artist, 'codec': codec, 'bits': bits,
            'bits_label': ' bit', 'khz': khz, 'kbps': kbps, 'channels': channels,
            'raw_sample_rate': sample_rate, 'sample_fmt': sample_fmt, 'duration': duration,
            'bit_rate': bit_rate, 'bit_rate_estimated': bit_rate_estimated,
            'album': _pick(tags, 'album', 'talb', '©alb'),
            'year': _pick(tags, 'date', 'tdrc', 'tyer', '©day', 'year'),
            'genre': _pick(tags, 'genre', 'tcon', '©gen'),
            'track': _pick(tags, 'tracknumber', 'trck', 'trkn'),
            'comment': _pick(tags, 'comment', 'comm', '©cmt'),
            'encoder': _pick(tags, 'encoder', 'tsse', 'tenc', '©too', 'encoded_by'),
            'file_size': file_size, 'has_cover': _has_cover(audio),
            'format_name': _FORMAT_LABELS.get(ext, codec),
            'file_path': file_path,
        }
    except Exception:
        return None


def _ffprobe_metadata(file_path):
    """ffprobe 实现（格式覆盖最全，但每首要 fork 一个进程）。"""
    if not FFPROBE_PATH:
        return None

    cmd = [FFPROBE_PATH, '-v', 'quiet', '-print_format', 'json',
           '-show_format', '-show_streams', file_path]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        info = json.loads(result.stdout.decode('utf-8', errors='ignore'))
        format_info = info.get('format', {})
        streams = info.get('streams', [])
        audio_stream = next((s for s in streams if s.get('codec_type') == 'audio'), {})

        codec = audio_stream.get('codec_name', 'unknown').upper()
        sample_rate = int(audio_stream.get('sample_rate', 44100) or 44100)
        channels = audio_stream.get('channels', 2)
        stream_bit_rate = int(audio_stream.get('bit_rate', 0) or 0)
        format_bit_rate = int(format_info.get('bit_rate', 0) or 0)
        file_size = int(format_info.get('size', 0) or 0)

        try:
            duration = float(format_info.get('duration', 0) or audio_stream.get('duration', 0) or 0)
        except (TypeError, ValueError):
            duration = 0.0

        # ---- 码率修复：优先取音频流自身码率 ----
        # 容器码率(format.bit_rate)会把封面图 / ID3 / 歌词一起算进去，
        # 所以 320k 的 mp3 常被显示成 330~400kbps。音轨流码率才是真实值。
        bit_rate_estimated = False
        if stream_bit_rate > 0:
            bit_rate = stream_bit_rate
        elif format_bit_rate > 0:
            bit_rate = format_bit_rate
            bit_rate_estimated = True
        elif duration > 0 and file_size > 0:
            bit_rate = int(file_size * 8 / duration)
            bit_rate_estimated = True
        else:
            bit_rate = 0

        # ---- 位深（回归真实状态，避免显示浮点）----
        bits_raw = audio_stream.get('bits_per_raw_sample')
        sample_fmt = audio_stream.get('sample_fmt', '') or ''

        if codec in LOSSY_CODECS:
            if bits_raw and bits_raw not in ('N/A', '0', 0):
                bits = str(bits_raw)
            else:
                bits = '16'
            bits_label = " bit"
        else:
            if bits_raw and bits_raw not in ('N/A', '0', 0):
                bits = str(bits_raw)
            elif 's16' in sample_fmt:
                bits = '16'
            elif 's24' in sample_fmt:
                bits = '24'
            elif 's32' in sample_fmt:
                bits = '32'
            else:
                bits = '未知'
            bits_label = " bit"

        kbps = (f"≈{bit_rate // 1000} kbps" if bit_rate_estimated else f"{bit_rate // 1000} kbps") if bit_rate > 0 else "未知码率"
        khz = f"{sample_rate / 1000:g} kHz"
        raw_tags = format_info.get('tags', {}) or {}
        tags_lower = {}
        for k, v in raw_tags.items():
            if isinstance(v, (list, tuple)):
                v = ' / '.join(str(x) for x in v)
            tags_lower[str(k).lower()] = v
        title = tags_lower.get('title') or os.path.basename(file_path)
        artist = tags_lower.get('artist') or tags_lower.get('album_artist') or '未知艺术家'
        has_cover = any(s.get('codec_type') == 'video' for s in streams)

        return {
            'title': title, 'artist': artist, 'codec': codec, 'bits': bits,
            'bits_label': bits_label, 'khz': khz, 'kbps': kbps, 'channels': channels,
            'raw_sample_rate': sample_rate, 'sample_fmt': sample_fmt, 'duration': duration,
            'bit_rate': bit_rate, 'bit_rate_estimated': bit_rate_estimated,
            'album': tags_lower.get('album', ''), 'year': tags_lower.get('date') or tags_lower.get('year', ''),
            'genre': tags_lower.get('genre', ''), 'track': tags_lower.get('track', ''),
            'comment': tags_lower.get('comment', ''), 'encoder': tags_lower.get('encoder', ''),
            'file_size': file_size, 'has_cover': has_cover,
            'format_name': format_info.get('format_long_name', ''),
            'file_path': file_path,
        }
    except Exception:
        return None


def get_audio_metadata(file_path):
    """mutagen 优先（无子进程，实测快 10~19 倍），失败自动回退 ffprobe。

    文件不存在时直接返回 None：跨平台后缓存里常见"别处带来的失效路径"，
    这样会走"跳过该曲"分支，而不是假装能播。
    """
    try:
        if not os.path.exists(file_path):
            return None
    except Exception:
        pass
    meta = _mutagen_metadata(file_path)
    if meta:
        return meta
    return _ffprobe_metadata(file_path)


def print_pipeline_info(meta):
    """打印音频链路信息"""
    print("=" * 55)
    print(get_str("audio_info_title"))
    print("=" * 55)
    print(get_str("audio_track"))
    print(f"  {meta['codec']} {meta['bits']}{meta.get('bits_label', ' bit')} {meta['khz']} {meta['kbps']}")
    print(f"  {meta['channels']} 声道")
    if meta.get('duration'):
        print(f"  时长: {format_time(meta['duration'])}")
    print("")
    print(get_str("audio_decoder"))
    print(get_str("audio_decoder_val"))
    print("")
    print(get_str("audio_resample"))
    print(get_str("audio_resample_val", khz=meta['khz']))
    print("")
    print(get_str("audio_signal"))
    print(get_str("audio_signal_val", fmt=meta['sample_fmt']))
    print("=" * 55)


# ================= 新增：详细歌曲信息（info 命令） =================
def _human_size(num):
    try:
        num = float(num or 0)
    except (TypeError, ValueError):
        return "未知"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def print_detail_info(meta, index=None, total=None):
    """完整展示当前曲目的标签与音轨参数。"""
    if not meta:
        print(get_str("no_current_song"))
        return

    line = "=" * 55
    print()
    print(line)
    if index is not None and total:
        print(get_str("detail_title_index", index=index, total=total))
    else:
        print(get_str("detail_title"))
    print(line)
    print(f"  标题   : {meta.get('title') or '未知'}")
    print(f"  艺术家 : {meta.get('artist') or '未知'}")
    if meta.get('album'):
        print(f"  专辑   : {meta['album']}")
    if meta.get('year'):
        print(f"  年份   : {meta['year']}")
    if meta.get('genre'):
        print(f"  流派   : {meta['genre']}")
    if meta.get('track'):
        print(f"  音轨号 : {meta['track']}")
    if meta.get('comment'):
        print(f"  备注   : {meta['comment']}")
    print(f"  文件   : {os.path.basename(meta.get('file_path', ''))}")
    print(f"  格式   : {meta.get('format_name') or meta.get('codec', '未知')}")
    print(f"  音轨   : {meta['codec']} {meta['bits']}{meta.get('bits_label', ' bit')} "
          f"{meta['khz']} {meta['kbps']} {meta['channels']} 声道")
    if meta.get('duration'):
        print(f"  时长   : {format_time(meta['duration'])}")
    print(f"  大小   : {_human_size(meta.get('file_size'))}")
    print(f"  封面   : {'有' if meta.get('has_cover') else '无'}")
    try:
        from utils import output as _out
        print(f"  输出   : {_out.summary_line()}")
    except Exception:
        pass
    try:
        from core.config import _tmp_marker  # noqa: F401
    except Exception:
        pass
    try:
        from utils.usbdac import summary_line as _usb_summary  # noqa: F401
    except Exception:
        try:
            from utils.usbaudio import summary_line
            print(f"  输出   : {summary_line()}")
        except Exception:
            pass
    if meta.get('encoder'):
        print(f"  编码器 : {meta['encoder']}")
    print(f"  路径   : {meta.get('file_path', '')}")
    print(line)
    print()


# ================= 新增：统一扫描入口 =================
def scan_audio_files(folder_paths, recursive=True):
    """统一的扫描入口，返回排序后的文件列表。"""
    files = []
    for folder in folder_paths:
        if os.path.exists(folder):
            print(get_str("scanning", folder=folder))
            files.extend(get_audio_files(folder, recursive=recursive))
        else:
            print(get_str("path_not_exist", folder=folder))
    files.sort()
    return files