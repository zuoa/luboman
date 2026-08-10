"""B站投稿前的视频预处理：片头拼接。

片头按投稿账号（BiliAccount.intro_video_path）配置，schedule 投稿任务时对
file_list 的每个视频文件前拼接片头，产出派生文件（不注册 RecordFile，
与抖音版切片同一先例）。产物路径含片头路径 hash 并按 mtime 缓存复用——
同一录像投多个账号（片头不同）互不覆盖，重试/重复投稿不重复转码。
"""
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

from luboman.config import config

logger = logging.getLogger('luboman')


def _probe_streams(path: str) -> Optional[Dict[str, Any]]:
    """ffprobe 探测首个视频流/音频流参数与时长，失败返回 None。"""
    ffprobe_path = config.get('ffprobe_path', 'ffprobe')
    command = [
        ffprobe_path, '-v', 'error', '-print_format', 'json',
        '-show_streams', '-show_format', path,
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return None
        info = json.loads(proc.stdout)
    except Exception:
        return None
    streams = info.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    result: Dict[str, Any] = {'duration': None}
    try:
        result['duration'] = float((info.get('format') or {}).get('duration'))
    except (TypeError, ValueError):
        pass
    if video:
        result['video'] = {k: video.get(k) for k in ('codec_name', 'width', 'height', 'pix_fmt')}
    if audio:
        result['audio'] = {k: audio.get(k) for k in ('codec_name', 'sample_rate', 'channels')}
    return result


def _same_encoding(src_info: Optional[Dict[str, Any]], intro_info: Optional[Dict[str, Any]]) -> bool:
    """片头与录像编码参数完全一致才允许 -c copy 无损拼接。
    （编码不同时 -c copy 常产出时间戳错乱但退出码为 0 的假成功文件，必须先探测分流。）"""
    if not src_info or not intro_info:
        return False
    if not src_info.get('video') or not intro_info.get('video'):
        return False
    return src_info['video'] == intro_info['video'] and src_info.get('audio') == intro_info.get('audio')


def _run_ffmpeg(command: List[str], timeout: int, desc: str) -> None:
    logger.info('%s命令: %s', desc, ' '.join(command))
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or '').strip().splitlines()[-5:]
        raise RuntimeError(f'ffmpeg {desc} failed ({proc.returncode}): {" | ".join(tail)}')


def _concat_copy(intro: str, src: str, dst: str) -> None:
    """concat demuxer 无损拼接（要求片头与录像编码参数一致，照 concat_clips 模式）。"""
    ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')
    list_file = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False)
    try:
        with list_file:
            for piece in (intro, src):
                # concat demuxer 单引号转义
                list_file.write("file '%s'\n" % piece.replace("'", "'\\''"))
        _run_ffmpeg([
            ffmpeg_path, '-y', '-f', 'concat', '-safe', '0',
            '-i', list_file.name, '-c', 'copy', dst,
        ], timeout=3600, desc='片头拼接(copy)')
    finally:
        try:
            os.unlink(list_file.name)
        except OSError:
            pass


def _concat_reencode(intro: str, src: str, dst: str,
                     src_info: Optional[Dict[str, Any]], intro_info: Optional[Dict[str, Any]]) -> None:
    """重编码拼接：片头 scale+pad 到录像分辨率后 concat，统一 h264/aac/faststart mp4。"""
    ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')
    src_video = (src_info or {}).get('video') or {}
    width = src_video.get('width') or 1920
    height = src_video.get('height') or 1080
    normalize_v = (
        f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
        f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p'
    )
    normalize_a = 'aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo'

    inputs = ['-i', intro, '-i', src]
    filter_parts = [f'[0:v]{normalize_v}[v0]', f'[1:v]{normalize_v}[v1]']
    src_has_audio = bool((src_info or {}).get('audio'))
    intro_has_audio = bool((intro_info or {}).get('audio'))
    if src_has_audio:
        if intro_has_audio:
            filter_parts += [f'[0:a]{normalize_a}[a0]']
        else:
            # 片头无音轨：用静音补齐，保证 concat 后音轨连续
            inputs += ['-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo']
            filter_parts += [f'[2:a]{normalize_a}[a0]']
        filter_parts += [f'[1:a]{normalize_a}[a1]', '[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]']
        maps = ['-map', '[v]', '-map', '[a]']
        audio_opts = ['-c:a', 'aac']
    else:
        filter_parts += ['[v0][v1]concat=n=2:v=1:a=0[v]']
        maps = ['-map', '[v]']
        audio_opts = []

    command = [
        ffmpeg_path, '-y', *inputs,
        '-filter_complex', ';'.join(filter_parts), *maps,
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
        *audio_opts, '-movflags', '+faststart', dst,
    ]
    _run_ffmpeg(command, timeout=7200, desc='片头拼接(重编码)')


def ensure_intro_merged(src: str, intro: str) -> str:
    """产出（或复用）拼了片头的投稿文件，返回文件路径。失败抛异常。

    产物是派生文件，放源文件同级的 intro/ 目录下；文件名含片头路径 hash——
    同一录像投多个账号（片头不同）不会互相覆盖。已存在且不旧于源文件和片头时
    直接复用（照 ensure_douyin_clip 缓存规则）。
    """
    if not src or not os.path.isfile(src):
        raise ValueError(f'video file not found: {src}')
    if not intro or not os.path.isfile(intro):
        raise ValueError(f'intro video file not found: {intro}')

    base = os.path.splitext(os.path.basename(src))[0]
    intro_hash = hashlib.md5(intro.encode('utf-8')).hexdigest()[:6]
    out_dir = os.path.join(os.path.dirname(src), 'intro')
    dst = os.path.join(out_dir, f'{base}__i{intro_hash}.mp4')

    if os.path.isfile(dst) and os.path.getsize(dst) > 0 \
            and os.path.getmtime(dst) >= max(os.path.getmtime(src), os.path.getmtime(intro)):
        logger.info('复用已拼接片头的文件: %s', dst)
        return dst

    os.makedirs(out_dir, exist_ok=True)
    src_info = _probe_streams(src)
    intro_info = _probe_streams(intro)

    if _same_encoding(src_info, intro_info):
        try:
            _concat_copy(intro, src, dst)
            # copy 拼接可能产出时间戳错乱的假成功文件，校验时长 ≈ 片头+录像
            dst_info = _probe_streams(dst)
            expect = (src_info.get('duration') or 0) + (intro_info.get('duration') or 0)
            actual = (dst_info or {}).get('duration')
            if expect and actual and abs(actual - expect) <= 2.0:
                return dst
            logger.warning('copy 拼接时长校验失败(expect=%.1f actual=%s)，降级重编码: %s', expect, actual, dst)
        except Exception:
            logger.warning('copy 拼接失败，降级重编码: %s', src, exc_info=True)

    _concat_reencode(intro, src, dst, src_info, intro_info)
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError(f'intro merge produced empty file: {dst}')
    return dst


def prepend_intro_to_file_list(file_list: List[Dict[str, Any]], intro_path: Optional[str]) -> List[Dict[str, Any]]:
    """对投稿文件列表逐文件拼片头，返回新的 file_list。

    逐文件独立 try/except：单文件失败降级为原文件投稿，不阻塞整体。
    片头未配置或文件不存在时原样返回（默认不处理）。
    """
    if not intro_path:
        return file_list
    if not os.path.isfile(intro_path):
        logger.warning('片头文件不存在，按原文件投稿: %s', intro_path)
        return file_list

    merged = []
    for item in file_list or []:
        video = (item or {}).get('video')
        if not video or not os.path.isfile(video):
            merged.append(item)
            continue
        try:
            merged_video = ensure_intro_merged(video, intro_path)
            merged.append({**item, 'video': merged_video})
        except Exception:
            logger.error('片头拼接失败，按原文件投稿: %s', video, exc_info=True)
            merged.append(item)
    return merged
