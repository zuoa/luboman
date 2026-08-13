"""三分屏舞蹈片段探测与切片。

思路：主播跳舞时通常把直播画面开成「三分屏」——画面被两条竖直分界线分成
左中右三栏（不一定等宽），且三栏里是同一画面的复制/镜像/拉伸。因此用 OpenCV
对采样帧做竖直分界线检测，再校验三栏内容两两相似（普通视频中碰巧出现的
竖直边缘——树干、栏杆、网页/游戏 UI 边框——栏内容互不相关，会被此校验过滤），
把持续处于三分屏布局的时间区间找出来，再用 ffmpeg 切出这些区间并注册进文件管理。

本模块内所有视频处理函数都是同步、CPU 密集的，统一经 run_blocking 在线程池
执行，不阻塞 aiohttp 事件循环。
"""
import asyncio
import concurrent.futures
import glob
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta
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
    'dance_clip_boundary_gap_seconds': 10,    # 相邻分段允许的最大时间间隔，用于跨分段舞蹈拼接
    'dance_clip_panel_similarity': 0.6,       # 三栏内容两两相似度阈值（全部达标才算三分屏，0 关闭校验）
    'dance_clip_task_timeout_hours': 2.0,     # 任务无进度更新的最长小时数，超时由看门狗标记失败（可手动重试）
}

_INT_KEYS = {
    'dance_clip_analyze_width', 'dance_clip_hysteresis', 'dance_clip_concurrency',
}
_FLOAT_KEYS = {
    'dance_clip_sample_interval', 'dance_clip_line_strength_ratio',
    'dance_clip_line_height_ratio', 'dance_clip_border_margin',
    'dance_clip_min_segment_ratio', 'dance_clip_max_pair_ratio',
    'dance_clip_merge_gap_seconds', 'dance_clip_min_clip_seconds',
    'dance_clip_pad_seconds', 'dance_clip_boundary_gap_seconds',
    'dance_clip_panel_similarity', 'dance_clip_task_timeout_hours',
}
_BOOL_KEYS = {'dance_clip_accurate_cut'}

CLIP_SERIES_CODE_PREFIX = 'CLIP:'


class ClipTaskAborted(Exception):
    """切片任务被外部请求中止（看门狗超时 / 服务停止）。"""


# 任务中止标志：看门狗对超时任务 set，任务体在探测循环里定期检查。
_CLIP_TASK_ABORT_FLAGS: Dict[str, threading.Event] = {}
_CLIP_TASK_ABORT_LOCK = threading.Lock()


def _clip_task_abort_event(task_id: str) -> threading.Event:
    with _CLIP_TASK_ABORT_LOCK:
        return _CLIP_TASK_ABORT_FLAGS.setdefault(task_id, threading.Event())


def request_clip_task_abort(task_id: str) -> None:
    """请求中止任务（协作式：探测循环检查到标志后抛 ClipTaskAborted）。"""
    _clip_task_abort_event(task_id).set()


def clear_clip_task_abort(task_id: str) -> None:
    with _CLIP_TASK_ABORT_LOCK:
        _CLIP_TASK_ABORT_FLAGS.pop(task_id, None)

# 舞蹈切片投稿标题模板：支持 {room_name} {room_title} {seq} 占位符和
# strftime 时间格式（取切片开始时间）。全局配置项 dance_clip_title_template。
# 日结多 P 开启时作为分 P 标题；关闭时作为单条 B 站稿标题（塞进模板 {room_title}）。
DEFAULT_CLIP_TITLE_TEMPLATE = '【{room_name}】%Y年%m月%d日 %H时 舞蹈片段{seq}'

# 抖音切片标题模板（全局配置项 dance_clip_douyin_title_template）。
# 抖音标题上限 30 字（插件内截断），默认模板比B站的短。
DEFAULT_DOUYIN_CLIP_TITLE_TEMPLATE = '【{room_name}】舞蹈片段{seq}'

# B 站日结多 P 稿件标题。strftime 取切片所属自然日。
DEFAULT_DAILY_TITLE_TEMPLATE = '【{room_name}】%Y年%m月%d日 舞蹈切片'
DAILY_MERGE_MAX_PARTS = 100


class _MissingKeyDict(dict):
    """format_map 用：未知占位符渲染为空串，避免下游标题模板再次 .format 时报错。"""

    def __missing__(self, key):
        return ''


def format_clip_title(template, room_data, seq, begin_time=None) -> str:
    """渲染舞蹈切片投稿标题。

    占位符在切片开始时间上展开 strftime；模板非法时回退默认模板。
    """
    def render(tpl):
        values = _MissingKeyDict(room_data or {})
        values['seq'] = seq
        return tpl.format_map(values)

    template = (template or '').strip() or DEFAULT_CLIP_TITLE_TEMPLATE
    try:
        text = render(template)
    except (ValueError, IndexError):
        logger.warning('舞蹈切片标题模板非法: %r，回退默认模板', template)
        text = render(DEFAULT_CLIP_TITLE_TEMPLATE)
    ts = begin_time if isinstance(begin_time, datetime) else datetime.now()
    return ts.strftime(text)


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def is_daily_bili_merge_enabled() -> bool:
    """AUTO 舞蹈切片是否攒到下播/跨天再打成一条多 P。缺省开启。"""
    value = config.get('dance_clip_bili_daily_merge')
    if value is None or value == '':
        return True
    return _to_bool(value, default=True)


def _today() -> date:
    return datetime.now().date()


def _clip_date(clip: Dict[str, Any]) -> date:
    begin_time = clip.get('begin_time')
    if isinstance(begin_time, datetime):
        return begin_time.date()
    if isinstance(begin_time, date):
        return begin_time
    if isinstance(begin_time, str):
        text = begin_time.strip()
        for candidate in (text, text.replace('Z', '+00:00')):
            try:
                return datetime.fromisoformat(candidate).date()
            except ValueError:
                continue
        try:
            return datetime.strptime(text[:10], '%Y-%m-%d').date()
        except ValueError:
            pass
    return _today()


def _room_is_live(room_data: Optional[Dict[str, Any]]) -> bool:
    """优先看内存里的直播实例；下播时 is_living 会先于 DB live_state 变 0。"""
    room_id = (room_data or {}).get('id')
    if room_id is not None:
        try:
            from luboman.core.async_live import async_live_room_manager
            live = async_live_room_manager.live_rooms.get(str(room_id))
            if live is not None:
                return bool(getattr(live, 'is_living', False))
        except Exception:
            pass
    return int((room_data or {}).get('live_state') or 0) == 1


def _should_flush_date(day: date, today: date, room_is_live: bool) -> bool:
    if day < today:
        return True
    return day == today and not room_is_live


def _chunk_clips(clips: List[Dict[str, Any]], max_parts: int = DAILY_MERGE_MAX_PARTS) -> List[List[Dict[str, Any]]]:
    size = max(1, int(max_parts or DAILY_MERGE_MAX_PARTS))
    return [clips[index:index + size] for index in range(0, len(clips), size)]


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', (name or '').strip())
    name = name.strip(' .')
    return (name[:80] or 'clip')


def _ensure_part_title_path(src: str, title: str, clip_id) -> str:
    """在切片旁建标题软链，供 biliup 用文件名当分 P 名。失败则回退原路径。"""
    if not src or not os.path.isfile(src):
        return src
    dest_dir = os.path.join(os.path.dirname(os.path.abspath(src)), '.daily_parts')
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        logger.warning('无法创建分P标题目录 %s，回退原文件名', dest_dir, exc_info=True)
        return src

    ext = os.path.splitext(src)[1] or '.mp4'
    abs_src = os.path.abspath(src)
    candidates = [
        os.path.join(dest_dir, f'{_sanitize_filename(title)}{ext}'),
        os.path.join(dest_dir, f'{_sanitize_filename(title)}_{clip_id}{ext}'),
    ]
    for dest in candidates:
        if os.path.lexists(dest):
            try:
                if os.path.samefile(dest, abs_src):
                    return dest
            except OSError:
                continue
            continue
        try:
            os.symlink(abs_src, dest)
            return dest
        except OSError:
            logger.warning('创建分P标题软链失败 %s -> %s', dest, abs_src, exc_info=True)
            continue
    return src


def _titled_file_list(clips: List[Dict[str, Any]], room_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    title_template = config.get('dance_clip_title_template')
    file_list = []
    for seq, clip in enumerate(clips, start=1):
        part_title = format_clip_title(title_template, room_data, seq, clip.get('begin_time'))
        file_list.append({
            'id': clip['id'],
            'video': _ensure_part_title_path(clip.get('video'), part_title, clip['id']),
        })
    return file_list


_flush_lock: Optional[asyncio.Lock] = None


def _get_flush_lock() -> asyncio.Lock:
    global _flush_lock
    if _flush_lock is None:
        _flush_lock = asyncio.Lock()
    return _flush_lock


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


_SIM_PANEL_SIZE = (64, 64)  # 栏内容相似度比较前的统一缩放尺寸


def _ncc(a, b) -> float:
    """归一化互相关系数（[-1, 1]，1 为完全一致）。"""
    import numpy as np
    a = a.astype(np.float64).ravel()
    b = b.astype(np.float64).ravel()
    a = a - a.mean()
    b = b - b.mean()
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float((a @ b) / denom) if denom > 0 else 0.0


def _panel_similarities(gray, x1: int, x2: int) -> Optional[List[float]]:
    """计算左中右三栏两两内容相似度（直接对比与左右镜像对比取较大者）。

    三分屏舞蹈的三栏是同一画面的复制/镜像/拉伸，相似度接近 1；
    普通视频中碰巧出现的竖直边缘（树干、栏杆、UI 边框），栏内容互不相关，相似度接近 0。
    """
    import cv2

    height, width = gray.shape
    panels = []
    for lo, hi in ((0, x1), (x1, x2), (x2, width)):
        # 各向内缩一点，避免分界线本身与贴边元素干扰
        mx = max(1, int((hi - lo) * 0.04))
        my = max(1, int(height * 0.04))
        panel = gray[my:height - my, lo + mx:hi - mx]
        if panel.size == 0:
            return None
        panels.append(cv2.resize(panel, _SIM_PANEL_SIZE, interpolation=cv2.INTER_AREA))
    sims = []
    for i, j in ((0, 1), (1, 2), (0, 2)):
        a, b = panels[i], panels[j]
        sims.append(max(_ncc(a, b), _ncc(a, cv2.flip(b, 1))))
    return sims


def _analyze_frame(frame, params) -> Optional[Tuple[float, float]]:
    """判断单帧是否为三分屏布局（两条竖直分界线 + 三栏各自达到最小栏宽 + 三栏内容一致）。

    命中时返回归一化分界线位置 (x1r, x2r)（0~1，相对画面宽度）——供抖音竖屏
    精剪按线位裁中栏；未命中返回 None。
    """
    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    analyze_width = params['dance_clip_analyze_width']
    if width > analyze_width:
        scale = analyze_width / width
        frame = cv2.resize(frame, (analyze_width, max(1, int(height * scale))))
        height, width = frame.shape[:2]

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # 竖向边缘 + 竖直形态学闭运算：把断续竖线连成段，压制横向纹理。
    # 必须用 CV_16S 再取绝对值——CV_8U 会把负梯度（亮→暗跳变）截断为 0，
    # 漏掉一半方向的边缘。
    sobel = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
    edges = cv2.convertScaleAbs(sobel)
    _, edges = cv2.threshold(edges, 40, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, height // 32)))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    proj = edges.sum(axis=0).astype(np.float64)
    max_proj = proj.max()
    if max_proj <= 0:
        return None

    # 候选列：投影强度达标 且 列内边缘行覆盖率达标（竖线需纵向贯通）
    min_rows = params['dance_clip_line_height_ratio'] * height
    row_counts = (edges > 0).sum(axis=0)
    candidates = np.where(
        (proj >= params['dance_clip_line_strength_ratio'] * max_proj) &
        (row_counts >= min_rows)
    )[0]
    if len(candidates) < 2:
        return None

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
        return None

    # 枚举所有线位对，找满足三分屏约束的组合（比"取最强两条"更稳健：
    # 分屏内容自身可能含强竖线，两条分界线未必是投影最强的两条）。
    # 约束只限最小栏宽/贴边/最大间距，不做等宽校验（三分屏不一定平分）。
    border = params['dance_clip_border_margin'] * width
    min_seg = params['dance_clip_min_segment_ratio'] * width
    max_pair = params['dance_clip_max_pair_ratio'] * width
    sim_threshold = params['dance_clip_panel_similarity']
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            x1, x2 = lines[i], lines[j]
            if x1 < border or x2 > width - border:
                continue
            if min(x1, x2 - x1, width - x2) < min_seg:
                continue
            if (x2 - x1) > max_pair:
                continue
            if sim_threshold > 0:
                # 关键校验：三栏内容必须两两一致（同一画面复制/镜像），
                # 否则任意含两条竖直边缘的画面（树干、栏杆、网页/游戏 UI）都会误判。
                # 必须三对全部达标：只要求两对时，网页两侧大面积纯色留白
                # 也会高度相关而漏过。
                sims = _panel_similarities(gray, x1, x2)
                if sims is None or any(s < sim_threshold for s in sims):
                    continue
            return (x1 / width, x2 / width)
    return None


def _samples_to_intervals(hits: List[Tuple[float, Optional[Tuple[float, float]]]], duration: float, params) -> List[Tuple[float, float]]:
    """把 (时间点, 命中线位或None) 采样序列转成区间列表：滞回防抖 → 合并 → 丢弃过短 → padding。"""
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


def _aggregate_interval_lines(
    hits: List[Tuple[float, Optional[Tuple[float, float]]]],
    intervals: List[Tuple[float, float]],
) -> List[Optional[Tuple[float, float]]]:
    """按区间聚合采样线位（x1r/x2r 各取中位数），返回与 intervals 对齐的线位列表。

    区间内无有效线位样本（理论上不会发生——区间由命中样本构成）时返回 None，
    下游转码回退画面中 1/3。
    """
    import statistics

    result: List[Optional[Tuple[float, float]]] = []
    for start, end in intervals:
        xs1 = [lines[0] for t, lines in hits if lines and start <= t <= end]
        xs2 = [lines[1] for t, lines in hits if lines and start <= t <= end]
        if xs1 and xs2:
            result.append((statistics.median(xs1), statistics.median(xs2)))
        else:
            result.append(None)
    return result


def detect_three_split_intervals(
    video_path: str,
    params: Dict[str, Any],
    abort_event: Optional[threading.Event] = None,
) -> Tuple[List[Tuple[float, float]], float, List[Optional[Tuple[float, float]]]]:
    """探测视频中三分屏布局的持续区间。

    返回 (intervals, duration_sec, interval_lines)：intervals 为 [(start_sec, end_sec), ...]，
    interval_lines 与 intervals 对齐，为各区间聚合的归一化分界线位置（供竖屏精剪裁中栏）。

    顺序扫描 + grab 跳帧（只对采样帧解码），不依赖 seek——部分容器
    （尤其直播录像 flv）的 seek 不可靠，会导致采样帧全部读取失败。
    abort_event 置位时抛 ClipTaskAborted（长录像扫描可能耗时很久，供看门狗中止）。
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

        logger.info(
            '三分屏探测开始 %s: %.1f 分钟, 共约 %d 帧, 每 %d 帧采样',
            os.path.basename(video_path), duration / 60, int(frame_count), step,
        )

        hits: List[Tuple[float, Optional[Tuple[float, float]]]] = []
        index = 0
        last_progress_log = time.monotonic()
        while True:
            if abort_event is not None and index % 2000 == 0 and abort_event.is_set():
                raise ClipTaskAborted(f'任务已中止（已扫描 {index} 帧）')
            ok = cap.grab()
            if not ok:
                break
            if index % step == 0:
                ok, frame = cap.retrieve()
                t = index / fps if fps > 0 else float(index)
                lines = _analyze_frame(frame, params) if ok and frame is not None else None
                hits.append((t, lines))
            index += 1
            # 定期打印扫描进度——长录像全文件扫描可能耗时较久，便于排查卡住的任务
            now = time.monotonic()
            if now - last_progress_log >= 60:
                pct = (index / frame_count * 100) if frame_count > 0 else 0
                logger.info(
                    '三分屏探测中 %s: 已扫描 %d/%d 帧 (%.1f%%)',
                    os.path.basename(video_path), index, int(frame_count), pct,
                )
                last_progress_log = now

        if fps <= 0:
            duration = hits[-1][0] if hits else 0
        intervals = _samples_to_intervals(hits, duration, params)
        interval_lines = _aggregate_interval_lines(hits, intervals)
        logger.info(
            '三分屏探测完成 %s: 采样 %d 帧, 检出 %d 个区间',
            os.path.basename(video_path), len(hits), len(intervals),
        )
        return intervals, duration, interval_lines
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


def concat_clips(pieces: List[str], dst: str) -> None:
    """用 ffmpeg concat demuxer 无损拼接同源的若干片段（同一直播流的切片，编码参数一致）。"""
    if len(pieces) < 2:
        raise ValueError('concat_clips needs at least 2 pieces')
    ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    list_file = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False)
    try:
        with list_file:
            for piece in pieces:
                # concat demuxer 单引号转义
                list_file.write("file '%s'\n" % piece.replace("'", "'\\''"))
        command = [
            ffmpeg_path, '-y', '-f', 'concat', '-safe', '0',
            '-i', list_file.name, '-c', 'copy', dst,
        ]
        logger.info('拼接命令: %s', ' '.join(command))
        proc = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            tail = (proc.stderr or '').strip().splitlines()[-5:]
            raise RuntimeError(f'ffmpeg concat failed ({proc.returncode}): {" | ".join(tail)}')
        if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            raise RuntimeError(f'ffmpeg concat produced empty file: {dst}')
    finally:
        try:
            os.unlink(list_file.name)
        except OSError:
            pass


def _sanitize_dir_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', '_', (name or '').strip())
    return name.strip('._') or 'unknown'


def transcode_clip_for_douyin(src: str, dst: str, lines: Optional[Tuple[float, float]] = None, vertical: bool = False) -> None:
    """把切片转码为抖音可投的 mp4（h264/aac/faststart）。

    vertical=True 时按三分屏线位裁中栏转 9:16 竖屏（1080x1920）：
    lines 为归一化分界线 (x1r, x2r)（0~1，相对画面宽度），缺省回退画面中 1/3。
    滤镜链用 cover 策略（放大铺满后居中裁）——中栏约 0.59:1 略宽于 9:16，
    仅损失两侧约 5% 画面，主体无损，优于 pad 黑边。
    """
    ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    command = [ffmpeg_path, '-y', '-i', src]
    if vertical:
        x1r, x2r = lines if lines else (1.0 / 3, 2.0 / 3)
        vf = (
            f'crop=w=iw*{x2r - x1r:.6f}:h=ih:x=iw*{x1r:.6f}:y=0,'
            'scale=1080:1920:force_original_aspect_ratio=increase,'
            'crop=1080:1920'
        )
        command += ['-vf', vf]
    command += [
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
        '-c:a', 'aac', '-movflags', '+faststart', dst,
    ]
    logger.info('抖音版转码命令: %s', ' '.join(command))
    proc = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        tail = (proc.stderr or '').strip().splitlines()[-5:]
        raise RuntimeError(f'ffmpeg douyin transcode failed ({proc.returncode}): {" | ".join(tail)}')
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError(f'ffmpeg douyin transcode produced empty file: {dst}')


def ensure_douyin_clip(record: Dict[str, Any], room_name: Optional[str] = None, vertical: bool = False) -> str:
    """产出（或复用）切片的抖音版转码文件，返回文件路径。

    产物是派生文件，放 clips/{房间}/douyin/ 下、不注册为 RecordFile
    （避免污染文件管理与被二次切片）。已存在且不旧于源文件时直接复用——
    同一切片投多个抖音模板（多账号）只转码一次。
    """
    src = (record or {}).get('video')
    if not src or not os.path.isfile(src):
        raise ValueError(f'clip video file not found: {src}')

    base = os.path.splitext(os.path.basename(src))[0]
    suffix = '__v.mp4' if vertical else '__h.mp4'
    out_dir = os.path.join(
        get_video_dir(), 'clips',
        _sanitize_dir_name(room_name or f"room_{record.get('live_room_id')}"),
        'douyin',
    )
    dst = os.path.join(out_dir, f'{base}{suffix}')

    if os.path.isfile(dst) and os.path.getsize(dst) > 0 \
            and os.path.getmtime(dst) >= os.path.getmtime(src):
        logger.info('复用已转码的抖音版切片: %s', dst)
        return dst

    lines = None
    if vertical:
        upload_info = record.get('upload_info') or {}
        raw_lines = upload_info.get('three_split_lines')
        if isinstance(raw_lines, (list, tuple)) and len(raw_lines) == 2:
            try:
                lines = (float(raw_lines[0]), float(raw_lines[1]))
            except (TypeError, ValueError):
                lines = None
    transcode_clip_for_douyin(src, dst, lines=lines, vertical=vertical)
    return dst


def _build_clip_output_path(src: str, room_name: Optional[str], live_room_id, start: float, end: float, accurate: bool) -> str:
    clips_dir = os.path.join(get_video_dir(), 'clips', _sanitize_dir_name(room_name or f'room_{live_room_id}'))
    base = os.path.splitext(os.path.basename(src))[0]
    suffix = '.mp4' if accurate else (os.path.splitext(src)[1] or '.mp4')
    filename = f'{base}__clip_{int(start):06d}-{int(end):06d}{suffix}'
    return os.path.join(clips_dir, filename)


# ---- 跨分段边界拼接（仅 AUTO 任务） ----
# 一支舞蹈可能横跨两个连续录制分段（前一个文件尾部 + 后一个文件开头）。
# 贴文件尾的区间先「挂起」不切，等下一分段完成且开头仍是三分屏时拼接成一个切片；
# 无法拼接或直播结束时再物化为普通切片。挂起状态只在内存，进程重启即丢失
# （未终态 ClipTask 启动时会被 recover_interrupted_clip_tasks 置 FAILED）。
_PENDING_TAILS: Dict[int, Dict[str, Any]] = {}
_pending_lock = threading.Lock()


def _safe_remove(path) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _pop_pending_tail(live_room_id) -> Optional[Dict[str, Any]]:
    with _pending_lock:
        return _PENDING_TAILS.pop(live_room_id, None)


def _hold_pending_tail(live_room_id, pending: Dict[str, Any]) -> None:
    with _pending_lock:
        old = _PENDING_TAILS.pop(live_room_id, None)
        _PENDING_TAILS[live_room_id] = pending
    if old and old.get('stage') == 'chain':
        _safe_remove(old.get('video'))


def _temp_work_path(clips_dir: str, prefix: str, suffix: str) -> str:
    return os.path.join(clips_dir, f'{prefix}{uuid.uuid4().hex}{suffix}')


def _make_raw_pending(record_data: Dict[str, Any], room_name, start: float, end: float, params,
                      lines: Optional[Tuple[float, float]] = None) -> Dict[str, Any]:
    """由贴文件尾的区间构造挂起态（尚未切割）。lines 为该区间聚合的三分屏线位。"""
    accurate = params['dance_clip_accurate_cut']
    begin_time = record_data['begin_time']
    return {
        'stage': 'raw',
        'origin_record_id': record_data['id'],
        'origin_video': record_data['video'],
        'video': record_data['video'],
        'start': start,
        'end': end,
        'name_start': int(start),
        'total_duration': end - start,
        'begin_time': begin_time + timedelta(seconds=start) if begin_time else None,
        'file_end_time': record_data.get('end_time') or (
            begin_time + timedelta(seconds=end) if begin_time else None
        ),
        'live_room_id': record_data['live_room_id'],
        'room_name': room_name,
        'accurate': accurate,
        'suffix': '.mp4' if accurate else (os.path.splitext(record_data['video'])[1] or '.mp4'),
        'lines': lines,
    }


def _build_merged_clip_path(pending: Dict[str, Any]) -> str:
    clips_dir = os.path.join(
        get_video_dir(), 'clips',
        _sanitize_dir_name(pending.get('room_name') or f"room_{pending['live_room_id']}"),
    )
    base = os.path.splitext(os.path.basename(pending['origin_video']))[0]
    start = int(pending['name_start'])
    end = int(pending['name_start'] + pending['total_duration'])
    return os.path.join(clips_dir, f'{base}__clip_{start:06d}-{end:06d}{pending["suffix"]}')


def _register_pending_clip(pending: Dict[str, Any], dst: str, end_time) -> Dict[str, Any]:
    from luboman.database.db import DB
    payload: Dict[str, Any] = {
        'live_room_id': pending['live_room_id'],
        'begin_time': pending['begin_time'],
        'end_time': end_time,
        'video': dst,
        'duration_seconds': int(pending['total_duration']),
        'series_code': f"{CLIP_SERIES_CODE_PREFIX}{pending['origin_record_id']}",
    }
    lines = pending.get('lines')
    if lines:
        payload['upload_info'] = {'three_split_lines': [lines[0], lines[1]]}
    clip_record, _ = DB.upsert_clip_record_file(payload)
    return clip_record


def _finalize_pending_tail(pending: Dict[str, Any]) -> Dict[str, Any]:
    """把挂起的边界片段物化为普通切片（不再等待下一分段）。"""
    if pending['stage'] == 'raw':
        dst = _build_clip_output_path(
            pending['video'], pending.get('room_name'), pending['live_room_id'],
            pending['start'], pending['end'], pending['accurate'],
        )
        cut_clip(pending['video'], dst, pending['start'], pending['end'], pending['accurate'])
    else:
        dst = _build_merged_clip_path(pending)
        os.replace(pending['video'], dst)
    return _register_pending_clip(pending, dst, pending['file_end_time'])


def _absorb_head(pending: Dict[str, Any], record_data: Dict[str, Any], head,
                 continues: bool) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """挂起片段与当前分段的开头区间拼接。

    continues=True 表示开头区间贯穿整个当前分段（舞蹈仍未结束）：
    拼成中间链文件继续挂起；否则产出最终合并切片。
    返回 (clip_record or None, new_pending or None)。
    """
    accurate = pending['accurate']
    room_name = record_data.get('room_name') or pending.get('room_name')
    clips_dir = os.path.join(
        get_video_dir(), 'clips',
        _sanitize_dir_name(room_name or f"room_{pending['live_room_id']}"),
    )
    os.makedirs(clips_dir, exist_ok=True)

    piece_b = _temp_work_path(clips_dir, '.piece_', pending['suffix'])
    piece_a = None
    try:
        cut_clip(record_data['video'], piece_b, head[0], head[1], accurate)
        if pending['stage'] == 'raw':
            piece_a = _temp_work_path(clips_dir, '.piece_', pending['suffix'])
            cut_clip(pending['video'], piece_a, pending['start'], pending['end'], accurate)
        else:
            piece_a = pending['video']

        total = pending['total_duration'] + (head[1] - head[0])
        if continues:
            chain_path = _temp_work_path(clips_dir, '.chain_', pending['suffix'])
            concat_clips([piece_a, piece_b], chain_path)
            _safe_remove(piece_a)
            new_pending = {
                **pending,
                'stage': 'chain',
                'video': chain_path,
                'room_name': room_name,
                'total_duration': total,
                'file_end_time': record_data.get('end_time'),
            }
            return None, new_pending

        merged = {**pending, 'room_name': room_name, 'total_duration': total}
        dst = _build_merged_clip_path(merged)
        concat_clips([piece_a, piece_b], dst)
        _safe_remove(piece_a)
        begin_time = record_data['begin_time']
        end_time = begin_time + timedelta(seconds=head[1]) if begin_time else None
        clip_record = _register_pending_clip(merged, dst, end_time)
        logger.info('跨分段舞蹈拼接完成: %s (%.1fs)', dst, total)
        return clip_record, None
    finally:
        _safe_remove(piece_b)


def drain_pending_tail(live_room_id) -> Optional[int]:
    """直播结束时冲刷挂起的边界片段，返回切片 RecordFile id（无挂起返回 None）。"""
    pending = _pop_pending_tail(live_room_id)
    if pending is None:
        return None
    try:
        clip_record = _finalize_pending_tail(pending)
        logger.info('挂起的舞蹈边界片段已冲刷: %s', clip_record.get('video'))
        return clip_record.get('id')
    except Exception:
        logger.warning('冲刷挂起的舞蹈边界片段失败: room=%s', live_room_id, exc_info=True)
        return None


def run_clip_task(task_id: str) -> None:
    """切片任务体（同步）：逐个来源文件 探测 → 切割 → 注册进文件管理 → 回写进度。

    AUTO 任务额外做跨分段边界处理：贴文件尾区间挂起等待与下一分段拼接，
    本文件开头区间若与上一分段挂起区间相邻则拼接。
    """
    from luboman.database.db import DB
    from luboman.database.models import LiveRoom, RecordFile, db

    DB.mark_clip_task_running(task_id)
    task = DB.get_clip_task(task_id=task_id)
    params = load_detect_params(task.get('params'))
    accurate = params['dance_clip_accurate_cut']
    is_auto = (task.get('source') or 'MANUAL') == 'AUTO'
    boundary_gap = params['dance_clip_boundary_gap_seconds']
    touch = max(1.0, params['dance_clip_sample_interval'])

    source_ids = task.get('source_record_file_ids') or []
    total = len(source_ids)
    all_intervals: List[Dict[str, Any]] = []
    clip_record_file_ids: List[int] = []
    errors: List[str] = []
    abort_event = _clip_task_abort_event(task_id)

    for index, record_id in enumerate(source_ids):
        if abort_event.is_set():
            break
        try:
            with db.connection_context():
                record = RecordFile.get_by_id(record_id)
                record_data = {
                    'id': record.id,
                    'live_room_id': record.live_room_id,
                    'video': record.video,
                    'begin_time': record.begin_time,
                    'end_time': record.end_time,
                }
                room = LiveRoom.get_or_none(LiveRoom.id == record.live_room_id)
                room_name = room.room_name if room else None
            record_data['room_name'] = room_name

            src = record_data['video']
            if not src or not os.path.isfile(src):
                raise ValueError(f'video file not found: {src}')

            intervals, duration, interval_lines = detect_three_split_intervals(src, params, abort_event)
            # 区间 -> 聚合线位（供抖音竖屏精剪裁中栏），以区间元组为键
            lines_by_interval = {
                (s, e): lines for (s, e), lines in zip(intervals, interval_lines)
            }
            file_entry: Dict[str, Any] = {
                'record_file_id': record_id,
                'video': src,
                'intervals': [[round(s, 1), round(e, 1)] for s, e in intervals],
            }

            process_intervals = list(intervals)
            new_pending: Optional[Dict[str, Any]] = None
            tail = None
            if is_auto:
                head = process_intervals[0] if process_intervals and process_intervals[0][0] <= touch else None
                tail = process_intervals[-1] if process_intervals and process_intervals[-1][1] >= duration - touch else None

                pending = _pop_pending_tail(record_data['live_room_id'])
                if pending is not None:
                    gap_ok = (
                        head is not None
                        and record_data['begin_time'] is not None
                        and pending.get('file_end_time') is not None
                        and abs((record_data['begin_time'] - pending['file_end_time']).total_seconds()) <= boundary_gap
                    )
                    if gap_ok:
                        continues = tail is not None and tail is head
                        # 挂起态缺线位时补用当前头区间的（同源同布局，头段线位不变）
                        if pending.get('lines') is None and head is not None:
                            pending['lines'] = lines_by_interval.get(tuple(head))
                        finalized, new_pending = _absorb_head(pending, record_data, head, continues)
                        if finalized is not None:
                            clip_record_file_ids.append(finalized['id'])
                        process_intervals = process_intervals[1:]
                        if continues:
                            tail = None  # 尾部已并入新的挂起链
                    else:
                        # 无法拼接（开头不是舞蹈或分段间隔过大）：挂起片段物化为普通切片
                        finalized = _finalize_pending_tail(pending)
                        clip_record_file_ids.append(finalized['id'])

            for start, end in process_intervals:
                if is_auto and tail is not None and (start, end) == tuple(tail):
                    # 贴文件尾的区间挂起，等下一分段完成后再决定是否拼接
                    new_pending = _make_raw_pending(
                        record_data, room_name, start, end, params,
                        lines=lines_by_interval.get((start, end)),
                    )
                    continue
                dst = _build_clip_output_path(src, room_name, record_data['live_room_id'], start, end, accurate)
                cut_clip(src, dst, start, end, accurate)
                begin_time = record_data['begin_time'] + timedelta(seconds=start) if record_data['begin_time'] else None
                end_time = record_data['begin_time'] + timedelta(seconds=end) if record_data['begin_time'] else None
                clip_payload: Dict[str, Any] = {
                    'live_room_id': record_data['live_room_id'],
                    'begin_time': begin_time,
                    'end_time': end_time,
                    'video': dst,
                    'duration_seconds': int(end - start),
                    'series_code': f'{CLIP_SERIES_CODE_PREFIX}{record_id}',
                }
                interval_line = lines_by_interval.get((start, end))
                if interval_line:
                    clip_payload['upload_info'] = {'three_split_lines': [interval_line[0], interval_line[1]]}
                clip_record, _ = DB.upsert_clip_record_file(clip_payload)
                clip_record_file_ids.append(clip_record['id'])

            if is_auto and new_pending is not None:
                _hold_pending_tail(record_data['live_room_id'], new_pending)

            all_intervals.append(file_entry)
        except ClipTaskAborted as e:
            # 看门狗已把任务标记为失败，这里不再回写状态，直接退出任务体
            logger.warning('切片任务 %s 处理文件 %s 被中止: %s', task_id, record_id, e)
            return
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
            only_if_active=True,
        )
        return

    DB.finish_clip_task(
        task_id, True,
        clip_record_file_ids=clip_record_file_ids,
        intervals=all_intervals,
        error_message='; '.join(errors)[:2000] if errors else None,
        only_if_active=True,
    )


async def auto_submit_clip_records(clip_ids: List[int], live_room_id) -> None:
    """把切片逐个投稿到 B 站/抖音/网盘。

    B 站/抖音复用房间投稿模板（多模板即多账号）。网盘仅在「只投稿切片」开启且配置了
    upload_storage_platform 时上传——整录网盘仍由 EVENT_UPLOAD 负责。
    房间未开自动切片、或未配任何目的地时只入库不投稿。
    """
    from luboman.database.db import (
        DB,
        is_upload_clips_only,
        resolve_room_bili_template_ids,
        resolve_room_douyin_template_ids,
    )
    from luboman.core.async_upload import (
        UploadPriority,
        async_upload_scheduler,
        schedule_bili_submission,
        schedule_douyin_submission,
    )

    if not clip_ids or not live_room_id:
        return
    try:
        room_data = await run_blocking(DB.get_live_room_data, live_room_id)
    except Exception:
        logger.warning('舞蹈切片自动投稿：房间 %s 不存在', live_room_id)
        return
    if int(room_data.get('auto_dance_clip') or 0) != 1:
        logger.info('房间 %s 已关闭自动舞蹈切片，产出切片不再投稿', live_room_id)
        return

    storage_platform = (room_data.get('upload_storage_platform') or '').strip()
    upload_clips_to_storage = bool(storage_platform) and is_upload_clips_only(room_data)

    bili_template_ids = resolve_room_bili_template_ids(room_data)
    douyin_template_ids = resolve_room_douyin_template_ids(room_data)
    if not bili_template_ids and not douyin_template_ids and not upload_clips_to_storage:
        logger.info('房间 %s 未配置投稿模板或网盘，舞蹈切片仅入库不投稿', live_room_id)
        return
    bili_templates = (
        await run_blocking(DB.get_bili_templates_with_accounts, bili_template_ids)
        if bili_template_ids else []
    )
    douyin_templates = (
        await run_blocking(DB.get_douyin_templates_with_accounts, douyin_template_ids)
        if douyin_template_ids else []
    )
    if not bili_templates and not douyin_templates and not upload_clips_to_storage:
        logger.warning('房间 %s 配置的投稿模板均不可用，舞蹈切片仅入库不投稿', live_room_id)
        return

    bili_title_template = config.get('dance_clip_title_template')
    douyin_title_template = config.get('dance_clip_douyin_title_template')
    daily_merge = is_daily_bili_merge_enabled()
    if daily_merge and bili_templates:
        logger.info('房间 %s 已开启 B 站日结多P，%d 条切片先入库，等下播或跨天再打包', live_room_id, len(clip_ids))
    for seq, record_id in enumerate(clip_ids, start=1):
        try:
            record = await run_blocking(DB.get_record_file, record_id)
        except Exception:
            logger.warning('舞蹈切片自动投稿：切片记录 %s 不存在', record_id)
            continue
        video = record.get('video')
        if not video or not os.path.isfile(video):
            continue
        if not daily_merge:
            for template_info in bili_templates:
                clip_room_data = {
                    **room_data,
                    # 每个切片单独投稿，标题按全局模板渲染（含序号避免多稿同名）
                    'room_title': format_clip_title(bili_title_template, room_data, seq, record.get('begin_time')),
                    'bili_upload_template': template_info,
                    'bili_upload_template_id': template_info['id'],
                }
                try:
                    result = await schedule_bili_submission(
                        file_list=[{'id': record_id, 'video': video}],
                        room_data=clip_room_data,
                        source='AUTO',
                        priority=UploadPriority.HIGH,
                        metadata={'created_from': 'auto_dance_clip'},
                    )
                    logger.info('舞蹈切片投稿任务已创建: 模板=%s, %s', template_info.get('template_name'), result)
                except Exception:
                    logger.exception('舞蹈切片 %s 投稿任务创建失败: 模板=%s', record_id, template_info.get('template_name'))

        for template_info in douyin_templates:
            vertical = _to_bool(template_info.get('vertical_crop'), default=True)
            try:
                # 按模板配置转码：竖屏裁中栏 9:16 / 横版仅转 mp4（flv 抖音不接受）
                douyin_video = await run_blocking(
                    ensure_douyin_clip, record, room_data.get('room_name'), vertical,
                )
            except Exception:
                logger.exception('舞蹈切片 %s 抖音版转码失败，跳过该模板: 模板=%s', record_id, template_info.get('template_name'))
                continue
            clip_room_data = {
                **room_data,
                'room_title': format_clip_title(douyin_title_template, room_data, seq, record.get('begin_time')),
                'douyin_upload_template': template_info,
                'douyin_upload_template_id': template_info['id'],
            }
            try:
                result = await schedule_douyin_submission(
                    file_list=[{'id': record_id, 'video': douyin_video}],
                    room_data=clip_room_data,
                    source='AUTO',
                    priority=UploadPriority.HIGH,
                    metadata={
                        'created_from': 'auto_dance_clip',
                        'douyin_upload_template_id': template_info['id'],
                        'origin_video': video,
                    },
                )
                logger.info('舞蹈切片抖音投稿任务已创建: 模板=%s, %s', template_info.get('template_name'), result)
            except Exception:
                logger.exception('舞蹈切片 %s 抖音投稿任务创建失败: 模板=%s', record_id, template_info.get('template_name'))

        if upload_clips_to_storage:
            try:
                result = await async_upload_scheduler.schedule_upload_simple(
                    platform=storage_platform,
                    file_list=[{'id': record_id, 'video': video}],
                    room_data=room_data,
                    priority=UploadPriority.HIGH,
                    metadata={'created_from': 'auto_dance_clip'},
                )
                if result:
                    logger.info(
                        '舞蹈切片网盘上传任务已创建: 平台=%s, 切片=%s, %s',
                        storage_platform, record_id, result,
                    )
                else:
                    logger.warning(
                        '舞蹈切片网盘上传未入队: 平台=%s, 切片=%s',
                        storage_platform, record_id,
                    )
            except Exception:
                logger.exception(
                    '舞蹈切片 %s 网盘上传任务创建失败: 平台=%s',
                    record_id, storage_platform,
                )


async def _submit_daily_bili_batch(
    room_data: Dict[str, Any],
    day: date,
    clips: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    from luboman.database.db import DB, resolve_room_bili_template_ids
    from luboman.core.async_upload import UploadPriority, schedule_bili_submission

    template_ids = resolve_room_bili_template_ids(room_data)
    templates = (
        await run_blocking(DB.get_bili_templates_with_accounts, template_ids)
        if template_ids else []
    )
    if not templates:
        logger.info('房间 %s 未配置可用 B 站模板，跳过 %s 日结打包', room_data.get('id'), day)
        return []

    clips = sorted(
        clips,
        key=lambda item: (item.get('begin_time') or datetime.min, item.get('id') or 0),
    )
    chunks = _chunk_clips(clips)
    day_begin = datetime.combine(day, datetime.min.time())
    daily_template = config.get('dance_clip_daily_title_template') or DEFAULT_DAILY_TITLE_TEMPLATE
    scheduled = []
    for index, chunk in enumerate(chunks, start=1):
        title = format_clip_title(daily_template, room_data, index, day_begin)
        if len(chunks) > 1 and index > 1:
            title = f'{title}({index})'
        file_list = _titled_file_list(chunk, room_data)
        for template_info in templates:
            upload_template = {**template_info, 'title': '{room_title}'}
            clip_room_data = {
                **room_data,
                'room_title': title,
                'bili_upload_template': upload_template,
                'bili_upload_template_id': template_info['id'],
            }
            try:
                result = await schedule_bili_submission(
                    file_list=file_list,
                    room_data=clip_room_data,
                    source='AUTO',
                    priority=UploadPriority.HIGH,
                    metadata={
                        'created_from': 'auto_dance_clip_daily',
                        'daily_date': day.isoformat(),
                        'part_index': index,
                    },
                )
                logger.info(
                    '舞蹈切片日结投稿已创建: 房间=%s 日期=%s 模板=%s 分P=%d %s',
                    room_data.get('id'), day, template_info.get('template_name'),
                    len(file_list), result,
                )
                scheduled.append(result)
            except Exception:
                logger.exception(
                    '舞蹈切片日结投稿失败: 房间=%s 日期=%s 模板=%s',
                    room_data.get('id'), day, template_info.get('template_name'),
                )
    return scheduled


async def flush_daily_bili_clip_batches(live_room_id=None) -> List[Dict[str, Any]]:
    """把未投 B 站的舞蹈切片按主播+自然日打包成多 P。

    昨天及更早的批次一律冲刷（跨午夜仍在播也发昨天的）。
    当天批次仅在房间已下播时冲刷。
    """
    if not is_daily_bili_merge_enabled():
        return []

    from luboman.database.db import DB

    async with _get_flush_lock():
        try:
            clips = await run_blocking(DB.list_pending_daily_bili_clips, live_room_id)
        except Exception:
            logger.exception('查询待日结舞蹈切片失败')
            return []
        if not clips:
            return []

        by_room: Dict[Any, List[Dict[str, Any]]] = {}
        for clip in clips:
            by_room.setdefault(clip.get('live_room_id'), []).append(clip)

        today = _today()
        scheduled: List[Dict[str, Any]] = []
        for room_id, room_clips in by_room.items():
            if not room_id:
                continue
            try:
                room_data = await run_blocking(DB.get_live_room_data, room_id)
            except Exception:
                logger.warning('日结打包：房间 %s 不存在', room_id)
                continue
            if int(room_data.get('auto_dance_clip') or 0) != 1:
                logger.info('房间 %s 已关闭自动舞蹈切片，跳过日结打包', room_id)
                continue
            room_is_live = _room_is_live(room_data)
            by_day: Dict[date, List[Dict[str, Any]]] = {}
            for clip in room_clips:
                by_day.setdefault(_clip_date(clip), []).append(clip)
            for day, day_clips in sorted(by_day.items()):
                if not _should_flush_date(day, today, room_is_live):
                    continue
                scheduled.extend(await _submit_daily_bili_batch(room_data, day, day_clips))
        return scheduled


async def _auto_submit_task_clips(task_id: str) -> None:
    """AUTO 切片任务成功后，把产出的切片逐个投稿。"""
    from luboman.database.db import DB

    try:
        task = await run_blocking(DB.get_clip_task, task_id)
    except Exception:
        logger.warning('读取切片任务 %s 失败，跳过自动投稿', task_id, exc_info=True)
        return
    if (task.get('source') or 'MANUAL') != 'AUTO' or task.get('status') != 'SUCCESS':
        return
    live_room_id = task.get('live_room_id')
    await auto_submit_clip_records(task.get('clip_record_file_ids') or [], live_room_id)
    if is_daily_bili_merge_enabled() and live_room_id:
        await flush_daily_bili_clip_batches(live_room_id)


class AsyncClipScheduler:
    """切片任务调度器：asyncio 队列 + 少量 worker，任务体经 run_blocking 在独立线程池执行。"""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.running = False
        self.workers: List[asyncio.Task] = []
        self.watchdog: Optional[asyncio.Task] = None
        self.executor: Optional[concurrent.futures.ThreadPoolExecutor] = None

    async def start(self):
        if self.running:
            return
        self.running = True
        concurrency = load_detect_params()['dance_clip_concurrency']
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix='clip-task',
        )
        # 清理上次进程遗留的跨分段拼接临时文件
        clips_root = os.path.join(get_video_dir(), 'clips')
        for stale in glob.glob(os.path.join(clips_root, '*', '.piece_*')) + \
                     glob.glob(os.path.join(clips_root, '*', '.chain_*')):
            _safe_remove(stale)
        for i in range(concurrency):
            self.workers.append(asyncio.create_task(self._worker(), name=f'clip-worker-{i}'))
        self.watchdog = asyncio.create_task(self._watchdog(), name='clip-watchdog')
        logger.info('切片任务调度器启动，并发: %d', concurrency)

    async def stop(self):
        if not self.running and not self.workers:
            return
        self.running = False
        tasks = list(self.workers)
        if self.watchdog:
            tasks.append(self.watchdog)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning('切片任务工作器停止超时')
        self.workers.clear()
        self.watchdog = None
        if self.executor:
            self.executor.shutdown(wait=False)
            self.executor = None
        logger.info('切片任务调度器已关闭')

    async def _watchdog(self):
        """看门狗：RUNNING 任务长时间无进度更新（卡在 OpenCV 扫描/ffmpeg）时，
        请求中止并标记失败，避免任务永远挂死。阈值取 dance_clip_task_timeout_hours。"""
        from luboman.database.db import DB

        while self.running:
            try:
                await asyncio.sleep(60)
                timeout_hours = load_detect_params()['dance_clip_task_timeout_hours']
                if timeout_hours <= 0:
                    continue
                stale_ids = await run_blocking(DB.list_stale_running_clip_tasks, timeout_hours)
                for task_id in stale_ids:
                    logger.warning(
                        '切片任务 %s 超过 %.1f 小时无进度更新，标记失败并请求中止',
                        task_id, timeout_hours,
                    )
                    request_clip_task_abort(task_id)
                    await run_blocking(
                        DB.finish_clip_task, task_id, False,
                        error_message=f'任务超时：超过 {timeout_hours:g} 小时无进度更新，已自动终止，可点击重试',
                        only_if_active=True,
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error('切片任务看门狗异常', exc_info=True)

    async def _worker(self):
        while self.running:
            try:
                try:
                    task_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    clear_clip_task_abort(task_id)
                    await run_blocking(run_clip_task, task_id, executor=self.executor)
                except Exception as e:
                    logger.error('切片任务 %s 执行异常: %s', task_id, e, exc_info=True)
                    try:
                        from luboman.database.db import DB
                        await run_blocking(
                            DB.finish_clip_task, task_id, False, None, None, str(e),
                            only_if_active=True,
                        )
                    except Exception:
                        logger.warning('切片任务失败态回写失败: %s', task_id, exc_info=True)
                else:
                    # 投稿失败不应把切片任务标为失败
                    try:
                        await _auto_submit_task_clips(task_id)
                    except Exception:
                        logger.warning('切片任务 %s 自动投稿异常', task_id, exc_info=True)
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error('切片任务工作器错误: %s', e, exc_info=True)

    async def schedule(self, file_ids: List[int], live_room_id=None, room_name=None, params: Optional[Dict[str, Any]] = None, source: str = 'MANUAL') -> Dict[str, Any]:
        """创建切片任务记录并排入队列。source: MANUAL手动探测 / AUTO录制分段自动触发。"""
        if not self.running:
            raise RuntimeError('clip scheduler is not running, please start the service via async_main.py')

        from luboman.database.db import DB

        task_id = str(uuid.uuid4())
        await run_blocking(DB.create_clip_task, {
            'task_id': task_id,
            'source': source,
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

    async def retry(self, task_id: str) -> Dict[str, Any]:
        """把失败的任务重置为 PENDING 并重新排队（沿用原任务 ID 与参数快照）。"""
        if not self.running:
            raise RuntimeError('clip scheduler is not running')

        from luboman.database.db import DB

        task = await run_blocking(DB.reset_clip_task_for_retry, task_id)
        clear_clip_task_abort(task_id)
        try:
            self.queue.put_nowait(task_id)
        except asyncio.QueueFull:
            message = 'clip task queue is full'
            await run_blocking(DB.finish_clip_task, task_id, False, None, None, message)
            raise RuntimeError(message)
        logger.info('切片任务 %s 已重新排队（来源文件 %d 个）', task_id, task.get('record_file_count') or 0)
        return {'task_id': task_id, 'file_count': task.get('record_file_count') or 0}

    async def flush_pending_tail(self, live_room_id) -> None:
        """直播结束：把挂起的跨分段边界片段物化为普通切片，并按 AUTO 规则投稿。"""
        if not live_room_id:
            return
        clip_id = await run_blocking(drain_pending_tail, live_room_id, executor=self.executor)
        if clip_id:
            await auto_submit_clip_records([clip_id], live_room_id)
        if is_daily_bili_merge_enabled():
            await flush_daily_bili_clip_batches(live_room_id)


# 全局切片任务调度器实例
clip_scheduler = AsyncClipScheduler()
