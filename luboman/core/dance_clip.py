"""三分屏舞蹈片段探测与切片。

思路：主播跳舞时通常把直播画面开成「三分屏」——画面被两条竖直分界线分成
左中右三栏（不一定等宽）。因此用 OpenCV 对采样帧做竖直分界线检测，把持续
处于三分屏布局的时间区间找出来，再用 ffmpeg 切出这些区间并注册进文件管理。

本模块内所有视频处理函数都是同步、CPU 密集的，统一经 run_blocking 在线程池
执行，不阻塞 aiohttp 事件循环。
"""
import asyncio
import concurrent.futures
import logging
import os
import re
import subprocess
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from luboman.config import config
from luboman.core.async_utils import run_blocking
from luboman.core.utils import get_video_dir

logger = logging.getLogger('luboman')

# 探测参数默认值。GlobalConfig 中的值均为字符串，load_detect_params 负责类型转换。
DEFAULT_DETECT_PARAMS: Dict[str, Any] = {
    'dance_clip_sample_interval': 2.0,        # 采样间隔（秒）
    'dance_clip_analyze_width': 480,          # 分析帧宽度（等比缩放）
    'dance_clip_line_strength_ratio': 0.5,    # 列投影强度阈值（相对最大值）
    'dance_clip_line_height_ratio': 0.6,      # 竖线行覆盖率阈值
    'dance_clip_border_margin': 0.08,         # 分界线不得贴近画面边缘的比例
    'dance_clip_min_segment_ratio': 0.15,     # 三栏各自的最小栏宽比例（只约束下限，不要求等宽）
    'dance_clip_max_pair_ratio': 0.85,        # 两条分界线的最大间距比例（防两侧装饰条误判）
    'dance_clip_hysteresis': 2,               # 滞回采样点数（防抖）
    'dance_clip_merge_gap_seconds': 30,       # 相邻区间间隔小于该值则合并
    'dance_clip_min_clip_seconds': 60,        # 最短切片时长，更短的丢弃
    'dance_clip_pad_seconds': 2,              # 区间头尾各扩展的秒数
    'dance_clip_accurate_cut': False,         # 精确切割（重编码）开关
    'dance_clip_concurrency': 1,              # 切片任务并发数（OpenCV 吃 CPU，默认 1）
}

_INT_KEYS = {
    'dance_clip_analyze_width', 'dance_clip_hysteresis', 'dance_clip_concurrency',
}
_FLOAT_KEYS = {
    'dance_clip_sample_interval', 'dance_clip_line_strength_ratio',
    'dance_clip_line_height_ratio', 'dance_clip_border_margin',
    'dance_clip_min_segment_ratio', 'dance_clip_max_pair_ratio',
    'dance_clip_merge_gap_seconds', 'dance_clip_min_clip_seconds',
    'dance_clip_pad_seconds',
}
_BOOL_KEYS = {'dance_clip_accurate_cut'}

CLIP_SERIES_CODE_PREFIX = 'CLIP:'


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def load_detect_params(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并 默认值 < GlobalConfig < 调用方覆盖，并做类型转换。"""
    params = dict(DEFAULT_DETECT_PARAMS)
    for key in DEFAULT_DETECT_PARAMS:
        if config.get(key) is not None:
            params[key] = config.get(key)
    for key, value in (overrides or {}).items():
        if key in DEFAULT_DETECT_PARAMS and value is not None:
            params[key] = value

    for key in _INT_KEYS:
        try:
            params[key] = int(params[key])
        except (TypeError, ValueError):
            params[key] = DEFAULT_DETECT_PARAMS[key]
    for key in _FLOAT_KEYS:
        try:
            params[key] = float(params[key])
        except (TypeError, ValueError):
            params[key] = DEFAULT_DETECT_PARAMS[key]
    for key in _BOOL_KEYS:
        params[key] = _to_bool(params[key], DEFAULT_DETECT_PARAMS[key])

    params['dance_clip_sample_interval'] = max(0.5, params['dance_clip_sample_interval'])
    params['dance_clip_analyze_width'] = max(160, params['dance_clip_analyze_width'])
    params['dance_clip_hysteresis'] = max(1, params['dance_clip_hysteresis'])
    params['dance_clip_concurrency'] = min(4, max(1, params['dance_clip_concurrency']))
    return params


def _analyze_frame(frame, params) -> bool:
    """判断单帧是否为三分屏布局（两条竖直分界线 + 三栏各自达到最小栏宽）。"""
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    analyze_width = params['dance_clip_analyze_width']
    if width > analyze_width:
        scale = analyze_width / width
        frame = cv2.resize(frame, (analyze_width, max(1, int(height * scale))))
        height, width = frame.shape[:2]

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # 竖向边缘 + 竖直形态学闭运算：把断续竖线连成段，压制横向纹理
    edges = cv2.Sobel(gray, cv2.CV_8U, 1, 0, ksize=3)
    _, edges = cv2.threshold(edges, 40, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, height // 32)))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    proj = edges.sum(axis=0).astype(np.float64)
    max_proj = proj.max()
    if max_proj <= 0:
        return False

    # 候选列：投影强度达标 且 列内边缘行覆盖率达标（竖线需纵向贯通）
    min_rows = params['dance_clip_line_height_ratio'] * height
    row_counts = (edges > 0).sum(axis=0)
    candidates = np.where(
        (proj >= params['dance_clip_line_strength_ratio'] * max_proj) &
        (row_counts >= min_rows)
    )[0]
    if len(candidates) < 2:
        return False

    # 相邻候选列聚类，取投影峰值列作为线位
    cluster_gap = max(2, int(width * 0.03))
    lines = []
    start = candidates[0]
    prev = candidates[0]
    for x in candidates[1:]:
        if x - prev > cluster_gap:
            seg = proj[start:prev + 1]
            lines.append(start + int(seg.argmax()))
            start = x
        prev = x
    seg = proj[start:prev + 1]
    lines.append(start + int(seg.argmax()))

    if len(lines) < 2:
        return False

    # 枚举所有线位对，找满足三分屏约束的组合（比"取最强两条"更稳健：
    # 分屏内容自身可能含强竖线，两条分界线未必是投影最强的两条）。
    # 约束只限最小栏宽/贴边/最大间距，不做等宽校验（三分屏不一定平分）。
    border = params['dance_clip_border_margin'] * width
    min_seg = params['dance_clip_min_segment_ratio'] * width
    max_pair = params['dance_clip_max_pair_ratio'] * width
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            x1, x2 = lines[i], lines[j]
            if x1 < border or x2 > width - border:
                continue
            if min(x1, x2 - x1, width - x2) < min_seg:
                continue
            if (x2 - x1) > max_pair:
                continue
            return True
    return False


def _samples_to_intervals(hits: List[Tuple[float, bool]], duration: float, params) -> List[Tuple[float, float]]:
    """把 (时间点, 是否三分屏) 采样序列转成区间列表：滞回防抖 → 合并 → 丢弃过短 → padding。"""
    interval = params['dance_clip_sample_interval']
    hysteresis = params['dance_clip_hysteresis']

    # 滞回：连续 hysteresis 个正采样才开区间，连续 hysteresis 个负采样才闭区间
    intervals = []
    open_start = None
    pos_run = 0
    neg_run = 0
    for t, hit in hits:
        if hit:
            pos_run += 1
            neg_run = 0
            if open_start is None and pos_run >= hysteresis:
                open_start = t - (hysteresis - 1) * interval
        else:
            neg_run += 1
            pos_run = 0
            if open_start is not None and neg_run >= hysteresis:
                intervals.append((open_start, t - (hysteresis - 1) * interval))
                open_start = None
    if open_start is not None:
        tail = hits[-1][0] + interval if hits else duration
        intervals.append((open_start, min(tail, duration)))

    # 合并相邻区间
    merge_gap = params['dance_clip_merge_gap_seconds']
    merged: List[List[float]] = []
    for start, end in sorted(intervals):
        if merged and start - merged[-1][1] < merge_gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    # 丢弃过短 + padding + clamp
    pad = params['dance_clip_pad_seconds']
    min_len = params['dance_clip_min_clip_seconds']
    result = []
    for start, end in merged:
        if end - start < min_len:
            continue
        result.append((max(0.0, start - pad), min(duration, end + pad)))

    # padding 后可能产生重叠，再合并一次
    final: List[List[float]] = []
    for start, end in sorted(result):
        if final and start <= final[-1][1]:
            final[-1][1] = max(final[-1][1], end)
        else:
            final.append([start, end])
    return [(s, e) for s, e in final]


def detect_three_split_intervals(video_path: str, params: Dict[str, Any]) -> List[Tuple[float, float]]:
    """探测视频中三分屏布局的持续区间，返回 [(start_sec, end_sec), ...]。

    顺序扫描 + grab 跳帧（只对采样帧解码），不依赖 seek——部分容器
    （尤其直播录像 flv）的 seek 不可靠，会导致采样帧全部读取失败。
    """
    import cv2

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f'cannot open video: {video_path}')

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = frame_count / fps if fps > 0 else 0
        if duration <= 0:
            raise ValueError(f'cannot determine video duration: {video_path}')

        interval = params['dance_clip_sample_interval']
        # 采样帧步长：fps 未知时退化为每 25 帧采一帧（约 1s @25fps）
        step = max(1, int(round(fps * interval))) if fps > 0 else 25

        hits: List[Tuple[float, bool]] = []
        index = 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if index % step == 0:
                ok, frame = cap.retrieve()
                t = index / fps if fps > 0 else float(index)
                hit = bool(ok and frame is not None and _analyze_frame(frame, params))
                hits.append((t, hit))
            index += 1

        if fps <= 0:
            duration = hits[-1][0] if hits else 0
        intervals = _samples_to_intervals(hits, duration, params)
        logger.info(
            '三分屏探测完成 %s: 采样 %d 帧, 检出 %d 个区间',
            os.path.basename(video_path), len(hits), len(intervals),
        )
        return intervals
    finally:
        cap.release()


def cut_clip(src: str, dst: str, start: float, end: float, accurate: bool = False) -> None:
    """用 ffmpeg 切出 [start, end) 区间。

    快速模式：-ss 在 -i 前 + -c copy，无损但起点对齐到 <= start 的关键帧
    （直播录像 GOP 一般 2-4s，配合区间 padding 可接受）。
    精确模式：重编码 libx264 + aac，帧级精确，统一输出 mp4。
    """
    ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if accurate:
        command = [
            ffmpeg_path, '-y', '-i', src,
            '-ss', f'{start:.3f}', '-to', f'{end:.3f}',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
            '-c:a', 'aac', dst,
        ]
    else:
        command = [
            ffmpeg_path, '-y',
            '-ss', f'{start:.3f}', '-to', f'{end:.3f}',
            '-i', src, '-c', 'copy', dst,
        ]
    logger.info('切片命令: %s', ' '.join(command))
    proc = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        tail = (proc.stderr or '').strip().splitlines()[-5:]
        raise RuntimeError(f'ffmpeg cut failed ({proc.returncode}): {" | ".join(tail)}')
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError(f'ffmpeg cut produced empty file: {dst}')


def _sanitize_dir_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', '_', (name or '').strip())
    return name.strip('._') or 'unknown'


def _build_clip_output_path(src: str, room_name: Optional[str], live_room_id, start: float, end: float, accurate: bool) -> str:
    clips_dir = os.path.join(get_video_dir(), 'clips', _sanitize_dir_name(room_name or f'room_{live_room_id}'))
    base = os.path.splitext(os.path.basename(src))[0]
    suffix = '.mp4' if accurate else (os.path.splitext(src)[1] or '.mp4')
    filename = f'{base}__clip_{int(start):06d}-{int(end):06d}{suffix}'
    return os.path.join(clips_dir, filename)


def run_clip_task(task_id: str) -> None:
    """切片任务体（同步）：逐个来源文件 探测 → 切割 → 注册进文件管理 → 回写进度。"""
    from luboman.database.db import DB
    from luboman.database.models import LiveRoom, RecordFile, db

    DB.mark_clip_task_running(task_id)
    task = DB.get_clip_task(task_id=task_id)
    params = load_detect_params(task.get('params'))
    accurate = params['dance_clip_accurate_cut']

    source_ids = task.get('source_record_file_ids') or []
    total = len(source_ids)
    all_intervals: List[Dict[str, Any]] = []
    clip_record_file_ids: List[int] = []
    errors: List[str] = []

    for index, record_id in enumerate(source_ids):
        try:
            with db.connection_context():
                record = RecordFile.get_by_id(record_id)
                record_data = {
                    'id': record.id,
                    'live_room_id': record.live_room_id,
                    'video': record.video,
                    'begin_time': record.begin_time,
                }
                room = LiveRoom.get_or_none(LiveRoom.id == record.live_room_id)
                room_name = room.room_name if room else None

            src = record_data['video']
            if not src or not os.path.isfile(src):
                raise ValueError(f'video file not found: {src}')

            intervals = detect_three_split_intervals(src, params)
            file_entry: Dict[str, Any] = {
                'record_file_id': record_id,
                'video': src,
                'intervals': [[round(s, 1), round(e, 1)] for s, e in intervals],
            }

            for start, end in intervals:
                dst = _build_clip_output_path(src, room_name, record_data['live_room_id'], start, end, accurate)
                cut_clip(src, dst, start, end, accurate)
                begin_time = record_data['begin_time'] + timedelta(seconds=start) if record_data['begin_time'] else None
                end_time = record_data['begin_time'] + timedelta(seconds=end) if record_data['begin_time'] else None
                clip_record, _ = DB.upsert_clip_record_file({
                    'live_room_id': record_data['live_room_id'],
                    'begin_time': begin_time,
                    'end_time': end_time,
                    'video': dst,
                    'duration_seconds': int(end - start),
                    'series_code': f'{CLIP_SERIES_CODE_PREFIX}{record_id}',
                })
                clip_record_file_ids.append(clip_record['id'])

            all_intervals.append(file_entry)
        except Exception as e:
            logger.warning('切片任务 %s 处理文件 %s 失败: %s', task_id, record_id, e, exc_info=True)
            errors.append(f'record {record_id}: {e}')
            all_intervals.append({
                'record_file_id': record_id,
                'error': str(e),
            })

        DB.update_clip_task_progress(
            task_id,
            progress=int(100 * (index + 1) / total) if total else 100,
            intervals=all_intervals,
        )

    if not clip_record_file_ids and errors and len(errors) == total:
        DB.finish_clip_task(
            task_id, False,
            clip_record_file_ids=clip_record_file_ids,
            intervals=all_intervals,
            error_message='; '.join(errors)[:2000],
        )
        return

    DB.finish_clip_task(
        task_id, True,
        clip_record_file_ids=clip_record_file_ids,
        intervals=all_intervals,
        error_message='; '.join(errors)[:2000] if errors else None,
    )


class AsyncClipScheduler:
    """切片任务调度器：asyncio 队列 + 少量 worker，任务体经 run_blocking 在独立线程池执行。"""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.running = False
        self.workers: List[asyncio.Task] = []
        self.executor: Optional[concurrent.futures.ThreadPoolExecutor] = None

    async def start(self):
        if self.running:
            return
        self.running = True
        concurrency = load_detect_params()['dance_clip_concurrency']
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix='clip-task',
        )
        for i in range(concurrency):
            self.workers.append(asyncio.create_task(self._worker(), name=f'clip-worker-{i}'))
        logger.info('切片任务调度器启动，并发: %d', concurrency)

    async def stop(self):
        if not self.running and not self.workers:
            return
        self.running = False
        for worker in self.workers:
            if not worker.done():
                worker.cancel()
        if self.workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.workers, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning('切片任务工作器停止超时')
        self.workers.clear()
        if self.executor:
            self.executor.shutdown(wait=False)
            self.executor = None
        logger.info('切片任务调度器已关闭')

    async def _worker(self):
        while self.running:
            try:
                try:
                    task_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await run_blocking(run_clip_task, task_id, executor=self.executor)
                except Exception as e:
                    logger.error('切片任务 %s 执行异常: %s', task_id, e, exc_info=True)
                    try:
                        from luboman.database.db import DB
                        await run_blocking(
                            DB.finish_clip_task, task_id, False, None, None, str(e),
                        )
                    except Exception:
                        logger.warning('切片任务失败态回写失败: %s', task_id, exc_info=True)
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error('切片任务工作器错误: %s', e, exc_info=True)

    async def schedule(self, file_ids: List[int], live_room_id=None, room_name=None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """创建切片任务记录并排入队列。"""
        if not self.running:
            raise RuntimeError('clip scheduler is not running, please start the service via async_main.py')

        from luboman.database.db import DB

        task_id = str(uuid.uuid4())
        await run_blocking(DB.create_clip_task, {
            'task_id': task_id,
            'source_record_file_ids': list(file_ids),
            'record_file_count': len(file_ids),
            'live_room_id': live_room_id,
            'room_name': room_name,
            'params': load_detect_params(params),
        })

        try:
            self.queue.put_nowait(task_id)
        except asyncio.QueueFull:
            message = 'clip task queue is full'
            await run_blocking(DB.finish_clip_task, task_id, False, None, None, message)
            raise RuntimeError(message)

        return {'task_id': task_id, 'file_count': len(file_ids)}


# 全局切片任务调度器实例
clip_scheduler = AsyncClipScheduler()
