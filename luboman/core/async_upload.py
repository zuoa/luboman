import asyncio
import itertools
import json
import logging
import time
import os
from typing import List, Dict, Any, Optional, Tuple, Set
from dataclasses import dataclass, field
from enum import Enum
import uuid

from luboman.core.async_utils import run_blocking

logger = logging.getLogger('luboman')


class UploadPriority(Enum):
    """上传优先级枚举"""
    HIGH = 1      # 高优先级（重要直播录像）
    NORMAL = 2    # 普通优先级
    LOW = 3       # 低优先级（批量处理）


@dataclass
class AsyncUploadTask:
    """异步上传任务数据结构"""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    platform: str = ''
    file_list: List[Dict[str, Any]] = field(default_factory=list)
    room_data: Dict[str, Any] = field(default_factory=dict)
    priority: UploadPriority = UploadPriority.NORMAL
    created_at: float = field(default_factory=time.time)
    max_retries: int = 3
    retry_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UploadResult:
    """上传结果数据结构"""
    task_id: str
    success: bool
    platform: str
    uploaded_files: List[str] = field(default_factory=list)
    failed_files: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    upload_time: float = 0.0
    total_size: int = 0
    upload_speed: float = 0.0  # MB/s
    raw_result: Any = None


class AsyncUploadScheduler:
    """异步上传调度器 - 优化上传系统的调度机制"""
    
    def __init__(self, max_concurrent_uploads: int = 3, 
                 max_concurrent_per_platform: int = 2):
        self.max_concurrent_uploads = max_concurrent_uploads
        self.max_concurrent_per_platform = max_concurrent_per_platform
        
        # 上传队列（按优先级排序）
        self.upload_queue = asyncio.PriorityQueue(maxsize=1000)
        self._task_sequence = itertools.count()
        self.running = False
        
        # 工作器任务
        self.upload_workers: List[asyncio.Task] = []
        self.retry_tasks: Set[asyncio.Task] = set()
        
        # 平台并发控制
        self.platform_semaphores: Dict[str, asyncio.Semaphore] = {}
        
        # 性能统计
        self.stats = {
            'tasks_total': 0,
            'tasks_success': 0,
            'tasks_failed': 0,
            'total_upload_time': 0.0,
            'total_uploaded_size': 0,
            'average_upload_speed': 0.0,
            'current_uploads': 0,
            'queue_size': 0
        }
        
        # 活跃上传任务跟踪
        self.active_uploads: Dict[str, AsyncUploadTask] = {}
        
        # 结果回调
        self.result_callbacks: List[callable] = []
    
    async def start(self):
        """启动异步上传调度器"""
        if self.running:
            return
        
        self.running = True
        logger.info(f"启动异步上传调度器，最大并发: {self.max_concurrent_uploads}")
        
        # 初始化平台信号量
        self._init_platform_semaphores()
        
        # 启动上传工作器
        for i in range(self.max_concurrent_uploads):
            worker = asyncio.create_task(
                self._upload_worker(f"upload-worker-{i}"),
                name=f"upload-worker-{i}"
            )
            self.upload_workers.append(worker)
        
        # 启动统计报告任务
        stats_task = asyncio.create_task(
            self._stats_reporter(),
            name="upload-stats-reporter"
        )
        self.upload_workers.append(stats_task)
        
        logger.info("异步上传调度器启动完成")
    
    async def stop(self):
        """停止异步上传调度器"""
        if not self.running and not self.upload_workers and not self.retry_tasks:
            return
        
        logger.info("正在关闭异步上传调度器...")
        self.running = False
        
        # 等待当前上传任务完成
        if self.active_uploads:
            logger.info(f"等待 {len(self.active_uploads)} 个上传任务完成...")
            
        # 停止所有工作器
        for worker in self.upload_workers:
            if not worker.done():
                worker.cancel()
        
        # 等待工作器停止
        if self.upload_workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.upload_workers, return_exceptions=True),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                logger.warning("上传工作器停止超时")

        if self.retry_tasks:
            logger.info(f"取消 {len(self.retry_tasks)} 个待重试上传任务")
            for retry_task in list(self.retry_tasks):
                if not retry_task.done():
                    retry_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.retry_tasks, return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("上传重试任务停止超时")

        self._drain_upload_queue()
        
        # 清理资源
        self.upload_workers.clear()
        self.retry_tasks.clear()
        self.active_uploads.clear()
        self.platform_semaphores.clear()
        
        logger.info("异步上传调度器已关闭")

    def _drain_upload_queue(self):
        """释放关闭时还未执行的上传任务引用。"""
        drained = 0
        while True:
            try:
                self.upload_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.upload_queue.task_done()
            drained += 1

        self.stats['queue_size'] = self.upload_queue.qsize()
        if drained:
            logger.info(f"已清理 {drained} 个未执行的上传任务")
    
    def _init_platform_semaphores(self):
        """初始化平台信号量"""
        platforms = ['biliweb', 'biliup-rs', 'alipan', 'bdpan', 'quark', 'telegram', 'local']

        for platform in platforms:
            self.platform_semaphores[platform] = asyncio.Semaphore(
                self.max_concurrent_per_platform
            )

        # 抖音走创作者平台模拟发布，风控敏感，固定串行（模块级 scheduler 在 import 时构造，
        # 此时 DB 配置未必已加载，故并发数硬编码而不读 config）
        self.platform_semaphores['douyin'] = asyncio.Semaphore(1)
    
    async def _upload_worker(self, worker_name: str):
        """上传工作器"""
        logger.debug(f"启动上传工作器: {worker_name}")
        
        while self.running:
            try:
                # 获取上传任务
                try:
                    priority_value, sequence, upload_task = await asyncio.wait_for(
                        self.upload_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                # 记录活跃任务
                self.active_uploads[upload_task.task_id] = upload_task
                self.stats['current_uploads'] += 1
                await self._mark_submission_task_running(upload_task)
                
                try:
                    # 执行上传
                    result = await self._execute_upload(upload_task, worker_name)

                    # 处理结果
                    await self._handle_upload_result(upload_task, result)
                finally:
                    # 清理活跃任务
                    self.active_uploads.pop(upload_task.task_id, None)
                    self.stats['current_uploads'] = max(0, self.stats['current_uploads'] - 1)

                    # 标记任务完成
                    self.upload_queue.task_done()
                
            except asyncio.CancelledError:
                logger.debug(f"上传工作器 {worker_name} 被取消")
                break
            except Exception as e:
                logger.error(f"上传工作器 {worker_name} 错误: {e}")
                self.stats['current_uploads'] = max(0, self.stats['current_uploads'] - 1)
    
    async def _execute_upload(self, upload_task: AsyncUploadTask, worker_name: str) -> UploadResult:
        """执行上传任务"""
        start_time = time.time()
        platform = upload_task.platform
        
        logger.info(
            f"开始上传任务 [{worker_name}] "
            f"平台: {platform}, "
            f"文件数: {len(upload_task.file_list)}, "
            f"优先级: {upload_task.priority.name}"
        )
        
        try:
            # 获取平台信号量
            semaphore = self.platform_semaphores.get(
                platform, 
                asyncio.Semaphore(self.max_concurrent_per_platform)
            )
            
            async with semaphore:
                # 计算总文件大小
                total_size = await self._calculate_total_size(upload_task.file_list)
                
                # 执行实际上传
                success, uploaded_files, failed_files, error_msg, raw_result = await self._perform_upload(
                    upload_task
                )
                
                upload_time = time.time() - start_time
                upload_speed = (total_size / 1024 / 1024) / upload_time if upload_time > 0 else 0
                
                # 更新统计
                self.stats['tasks_total'] += 1
                self.stats['total_upload_time'] += upload_time
                self.stats['total_uploaded_size'] += total_size if success else 0
                
                if success:
                    self.stats['tasks_success'] += 1
                else:
                    self.stats['tasks_failed'] += 1
                
                # 计算平均上传速度
                if self.stats['total_upload_time'] > 0:
                    self.stats['average_upload_speed'] = (
                        self.stats['total_uploaded_size'] / 1024 / 1024
                    ) / self.stats['total_upload_time']
                
                return UploadResult(
                    task_id=upload_task.task_id,
                    success=success,
                    platform=platform,
                    uploaded_files=uploaded_files,
                    failed_files=failed_files,
                    error_message=error_msg,
                    upload_time=upload_time,
                    total_size=total_size,
                    upload_speed=upload_speed,
                    raw_result=raw_result
                )
        
        except Exception as e:
            upload_time = time.time() - start_time
            error_msg = f"上传异常: {str(e)}"
            
            logger.error(f"上传任务失败 [{worker_name}]: {error_msg}")
            
            self.stats['tasks_total'] += 1
            self.stats['tasks_failed'] += 1
            
            return UploadResult(
                task_id=upload_task.task_id,
                success=False,
                platform=platform,
                error_message=error_msg,
                upload_time=upload_time
            )
    
    async def _calculate_total_size(self, file_list: List[Dict[str, Any]]) -> int:
        """计算文件总大小"""
        total_size = 0
        
        def calculate_size_sync():
            size = 0
            for file_info in file_list:
                file_path = file_info.get('video', '')
                if os.path.exists(file_path):
                    size += os.path.getsize(file_path)
            return size
        
        try:
            total_size = await run_blocking(calculate_size_sync)
        except Exception as e:
            logger.warning(f"计算文件大小失败: {e}")
        
        return total_size
    
    async def _perform_upload(self, upload_task: AsyncUploadTask) -> Tuple[bool, List[str], List[str], Optional[str], Any]:
        """执行实际上传操作"""
        platform = upload_task.platform
        file_list = upload_task.file_list
        
        uploaded_files = []
        failed_files = []
        error_message = None
        
        try:
            # 导入上传函数
            from luboman.core.upload import upload
            
            # 在线程中执行上传（因为原有上传系统是同步的）
            def upload_sync():
                if upload_task.room_data:
                    return upload(platform, file_list, room_data=upload_task.room_data)
                return upload(platform, file_list)
            
            result = await run_blocking(upload_sync)
            
            # 处理上传结果
            if isinstance(result, dict):
                explicit_success = result.get('success')
                code = result.get('code')
                if explicit_success is False or (
                    code is not None and str(code) not in ('0', '')
                ):
                    failed_files = [f.get('video', '') for f in file_list]
                    return False, [], failed_files, self._extract_upload_error_message(result), result

            if result:
                uploaded_files = [f.get('video', '') for f in file_list]
                return True, uploaded_files, [], None, result
            else:
                failed_files = [f.get('video', '') for f in file_list]
                return False, [], failed_files, "上传返回失败结果", result
        
        except Exception as e:
            error_message = str(e)
            failed_files = [f.get('video', '') for f in file_list]
            return False, [], failed_files, error_message, None

    @staticmethod
    def _extract_upload_error_message(result: Dict[str, Any]) -> str:
        for key in ('error_message', 'error', 'message'):
            value = result.get(key)
            if value:
                return str(value)

        output_tail = result.get('output_tail')
        if isinstance(output_tail, list) and output_tail:
            return '\n'.join(str(line) for line in output_tail[-20:])
        if output_tail:
            return str(output_tail)

        if result.get('exit_code') is not None:
            return f"上传进程退出码: {result.get('exit_code')}"
        if result.get('code') is not None:
            return f"上传接口返回 code={result.get('code')}"
        return "上传返回失败结果"

    def _is_submission_task(self, upload_task: AsyncUploadTask) -> bool:
        return bool((upload_task.metadata or {}).get('submission_task'))

    def _submission_task_id(self, upload_task: AsyncUploadTask) -> str:
        return (upload_task.metadata or {}).get('submission_task_id') or upload_task.task_id

    @staticmethod
    def _json_safe(value):
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except TypeError:
            return str(value)

    def _build_submission_result(self, result: UploadResult) -> Dict[str, Any]:
        return {
            'task_id': result.task_id,
            'success': result.success,
            'platform': result.platform,
            'uploaded_files': result.uploaded_files,
            'failed_files': result.failed_files,
            'error_message': result.error_message,
            'upload_time': result.upload_time,
            'total_size': result.total_size,
            'upload_speed': result.upload_speed,
            'raw_result': self._json_safe(result.raw_result),
        }

    async def _mark_submission_task_running(self, upload_task: AsyncUploadTask):
        if not self._is_submission_task(upload_task):
            return
        try:
            from luboman.database.db import DB

            await run_blocking(
                DB.mark_submission_task_running,
                self._submission_task_id(upload_task),
                upload_task.retry_count,
            )
        except Exception:
            logger.warning("投稿任务运行态回写失败: %s", upload_task.task_id, exc_info=True)

    async def _mark_submission_task_retrying(self, upload_task: AsyncUploadTask, result: UploadResult):
        if not self._is_submission_task(upload_task):
            return
        try:
            from luboman.database.db import DB

            await run_blocking(
                DB.mark_submission_task_retrying,
                self._submission_task_id(upload_task),
                upload_task.retry_count,
                result.error_message,
            )
        except Exception:
            logger.warning("投稿任务重试态回写失败: %s", upload_task.task_id, exc_info=True)

    async def _finish_submission_task(self, upload_task: AsyncUploadTask, result: UploadResult):
        if not self._is_submission_task(upload_task):
            return
        try:
            from luboman.database.db import DB

            await run_blocking(
                DB.finish_submission_task,
                self._submission_task_id(upload_task),
                result.success,
                self._build_submission_result(result),
                result.error_message,
            )
        except Exception:
            logger.warning("投稿任务完成态回写失败: %s", upload_task.task_id, exc_info=True)
    
    async def _handle_upload_result(self, upload_task: AsyncUploadTask, result: UploadResult):
        """处理上传结果"""
        if result.success:
            logger.info(
                f"上传成功 - 任务: {upload_task.task_id}, "
                f"平台: {result.platform}, "
                f"文件数: {len(result.uploaded_files)}, "
                f"耗时: {result.upload_time:.2f}s, "
                f"速度: {result.upload_speed:.2f}MB/s"
            )
        else:
            logger.warning(
                f"上传失败 - 任务: {upload_task.task_id}, "
                f"平台: {result.platform}, "
                f"错误: {result.error_message}"
            )
            
            # 重试逻辑
            if upload_task.retry_count < upload_task.max_retries:
                upload_task.retry_count += 1
                # 指数退避重试
                retry_delay = 2 ** upload_task.retry_count
                
                logger.info(
                    f"计划重试上传 - 任务: {upload_task.task_id}, "
                    f"重试次数: {upload_task.retry_count}/{upload_task.max_retries}, "
                    f"延迟: {retry_delay}s"
                )
                
                await self._mark_submission_task_retrying(upload_task, result)
                self._track_retry_task(upload_task, retry_delay)
            else:
                await self._finish_submission_task(upload_task, result)

        if result.success:
            await self._finish_submission_task(upload_task, result)
        
        # 调用结果回调
        for callback in self.result_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(upload_task, result)
                else:
                    callback(upload_task, result)
            except Exception as e:
                logger.error(f"上传结果回调失败: {e}")
    
    async def _schedule_retry(self, upload_task: AsyncUploadTask, delay: float):
        """计划重试上传"""
        await asyncio.sleep(delay)
        
        if self.running:
            await self.schedule_upload(upload_task)

    def _track_retry_task(self, upload_task: AsyncUploadTask, delay: float):
        retry_task = asyncio.create_task(
            self._schedule_retry(upload_task, delay),
            name=f"upload-retry-{upload_task.task_id}"
        )
        self.retry_tasks.add(retry_task)
        retry_task.add_done_callback(self.retry_tasks.discard)
    
    async def schedule_upload(self, upload_task: AsyncUploadTask) -> bool:
        """调度上传任务"""
        if not self.running:
            logger.warning("上传调度器未运行，忽略上传任务")
            return False
        
        try:
            # 使用优先级值作为队列排序键
            priority_value = upload_task.priority.value
            
            self.upload_queue.put_nowait((priority_value, next(self._task_sequence), upload_task))
            self.stats['queue_size'] = self.upload_queue.qsize()
            
            logger.debug(
                f"上传任务已加入队列 - "
                f"平台: {upload_task.platform}, "
                f"优先级: {upload_task.priority.name}, "
                f"队列大小: {self.stats['queue_size']}"
            )
            return True
            
        except asyncio.QueueFull:
            logger.error(f"上传队列已满，丢弃任务: {upload_task.task_id}")
            return False
    
    async def schedule_upload_simple(self, platform: str, file_list: List[Dict[str, Any]], 
                                   room_data: Optional[Dict[str, Any]] = None,
                                   priority: UploadPriority = UploadPriority.NORMAL,
                                   task_id: Optional[str] = None,
                                   max_retries: int = 3,
                                   metadata: Optional[Dict[str, Any]] = None):
        """简化的上传调度接口"""
        upload_task = AsyncUploadTask(
            task_id=task_id or str(uuid.uuid4()),
            platform=platform,
            file_list=file_list,
            room_data=room_data or {},
            priority=priority,
            max_retries=max_retries,
            metadata=metadata or {},
        )
        
        scheduled = await self.schedule_upload(upload_task)
        return upload_task.task_id if scheduled else None
    
    def add_result_callback(self, callback: callable):
        """添加结果回调函数"""
        self.result_callbacks.append(callback)
    
    def remove_result_callback(self, callback: callable):
        """移除结果回调函数"""
        if callback in self.result_callbacks:
            self.result_callbacks.remove(callback)
    
    async def _stats_reporter(self):
        """统计报告器"""
        while self.running:
            try:
                await asyncio.sleep(60)  # 每分钟报告一次
                
                if self.stats['tasks_total'] > 0:
                    success_rate = (self.stats['tasks_success'] / self.stats['tasks_total']) * 100
                    
                    logger.info(
                        f"上传统计 - "
                        f"总任务: {self.stats['tasks_total']}, "
                        f"成功: {self.stats['tasks_success']}, "
                        f"失败: {self.stats['tasks_failed']}, "
                        f"成功率: {success_rate:.1f}%, "
                        f"当前上传: {self.stats['current_uploads']}, "
                        f"队列大小: {self.stats['queue_size']}, "
                        f"平均速度: {self.stats['average_upload_speed']:.2f}MB/s"
                    )
                    
                    # 性能警告
                    if self.stats['queue_size'] > 50:
                        logger.warning(f"上传队列积压: {self.stats['queue_size']}")
                    
                    if success_rate < 80 and self.stats['tasks_total'] > 10:
                        logger.warning(f"上传成功率过低: {success_rate:.1f}%")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"上传统计报告失败: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            **self.stats,
            'queue_size': self.upload_queue.qsize(),
            'active_uploads_count': len(self.active_uploads),
            'active_upload_tasks': list(self.active_uploads.keys()),
            'retry_tasks_count': len(self.retry_tasks),
            'platform_limits': {
                platform: semaphore._value 
                for platform, semaphore in self.platform_semaphores.items()
            }
        }
    
    async def get_queue_status(self) -> Dict[str, Any]:
        """获取队列状态"""
        platform_counts = {}
        priority_counts = {}
        
        # 统计队列中的任务
        temp_tasks = []
        while not self.upload_queue.empty():
            try:
                item = self.upload_queue.get_nowait()
                temp_tasks.append(item)
                
                priority_value, sequence, upload_task = item
                platform = upload_task.platform
                priority_name = upload_task.priority.name
                
                platform_counts[platform] = platform_counts.get(platform, 0) + 1
                priority_counts[priority_name] = priority_counts.get(priority_name, 0) + 1
                
            except asyncio.QueueEmpty:
                break
        
        # 重新放回队列
        for item in temp_tasks:
            await self.upload_queue.put(item)
        
        return {
            'queue_size': len(temp_tasks),
            'platform_distribution': platform_counts,
            'priority_distribution': priority_counts,
            'active_uploads': len(self.active_uploads)
        }


def _json_data(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_record_file_ids(file_list: List[Dict[str, Any]]) -> List[int]:
    ids = []
    for file_info in file_list or []:
        row_id = _as_int(file_info.get('id') or file_info.get('record_file_id'))
        if row_id is not None:
            ids.append(row_id)
    return ids


async def schedule_submission(
    platform: str,
    file_list: List[Dict[str, Any]],
    room_data: Optional[Dict[str, Any]] = None,
    source: str = 'AUTO',
    priority: UploadPriority = UploadPriority.HIGH,
    metadata: Optional[Dict[str, Any]] = None,
    template_field: str = 'bili_upload_template',
) -> Dict[str, Any]:
    """创建投稿任务记录并排入异步上传调度器（平台无关）。

    platform 为上传插件注册名（如 biliup-rs / douyin）；template_field 为
    room_data 中模板上下文的键名（模板 id 字段约定为 f'{template_field}_id'）。
    模板 id/name 统一写入 SubmissionTask 的 bili_upload_template_id/name 列
    （历史列名，事实上已泛化为"模板 id/name"，避免加列迁移）。
    """
    if not async_upload_scheduler.running:
        raise RuntimeError('async upload scheduler is not running, please start the service via async_main.py')

    room_data = room_data or {}
    metadata = metadata or {}
    task_id = str(uuid.uuid4())

    from luboman.database.db import DB

    template_info = room_data.get(template_field) or {}
    record_file_ids = _extract_record_file_ids(file_list)
    task_metadata = {
        **metadata,
        'submission_task': True,
        'submission_task_id': task_id,
        'source': source,
    }

    await run_blocking(DB.create_submission_task, {
        'task_id': task_id,
        'source': source,
        'platform': platform,
        'priority': priority.name,
        'file_list': _json_data(file_list),
        'file_count': len(file_list or []),
        'record_file_ids': record_file_ids,
        'live_room_id': _as_int(room_data.get('id') or room_data.get('live_room_id')),
        'room_name': room_data.get('room_name'),
        'room_platform': room_data.get('room_platform'),
        'bili_upload_template_id': _as_int(
            room_data.get(f'{template_field}_id') or template_info.get('id')
        ),
        'bili_upload_template_name': template_info.get('template_name'),
        'uploader': platform,
        'max_retries': 3,
        'metadata': _json_data(metadata),
    })

    scheduled_task_id = await async_upload_scheduler.schedule_upload_simple(
        platform=platform,
        file_list=file_list,
        room_data=room_data,
        priority=priority,
        task_id=task_id,
        metadata=task_metadata,
    )

    if not scheduled_task_id:
        message = 'upload queue is full or scheduler stopped'
        await run_blocking(DB.mark_submission_task_failed, task_id, message)
        raise RuntimeError(message)

    return {
        'task_id': task_id,
        'file_count': len(file_list or []),
        'uploader': platform,
    }


async def schedule_bili_submission(
    file_list: List[Dict[str, Any]],
    room_data: Optional[Dict[str, Any]] = None,
    source: str = 'AUTO',
    priority: UploadPriority = UploadPriority.HIGH,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """创建B站投稿任务记录并排入异步上传调度器。"""
    from luboman.core.upload import resolve_bili_uploader

    # 片头（按投稿账号配置）：schedule 单点拼接，自动/手动两条投稿链路都汇聚于此；
    # 产物落盘缓存，调度器重试复用已记录的 file_list，不重复拼接
    room_data = room_data or {}
    intro_path = (
        (room_data.get('bili_upload_template') or {}).get('bili_account') or {}
    ).get('intro_video_path')
    if intro_path:
        from luboman.core.upload_prep import prepend_intro_to_file_list
        file_list = await run_blocking(prepend_intro_to_file_list, file_list, intro_path)

    return await schedule_submission(
        platform=resolve_bili_uploader(room_data),
        file_list=file_list,
        room_data=room_data,
        source=source,
        priority=priority,
        metadata=metadata,
        template_field='bili_upload_template',
    )


async def schedule_douyin_submission(
    file_list: List[Dict[str, Any]],
    room_data: Optional[Dict[str, Any]] = None,
    source: str = 'AUTO',
    priority: UploadPriority = UploadPriority.HIGH,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """创建抖音投稿任务记录并排入异步上传调度器。"""
    return await schedule_submission(
        platform='douyin',
        file_list=file_list,
        room_data=room_data,
        source=source,
        priority=priority,
        metadata=metadata,
        template_field='douyin_upload_template',
    )


# 上传事件处理器
class UploadEventHandler:
    """上传事件处理器 - 集成到异步事件系统"""
    
    def __init__(self, upload_scheduler: AsyncUploadScheduler):
        self.upload_scheduler = upload_scheduler
    
    async def handle_upload_event(self, event):
        """处理上传事件"""
        if getattr(event, 'room_id', None) is not None:
            return

        file_list = event.args[0] if event.args else []
        room_data = event.data.get('room_data', {})
        
        if file_list:
            await self.upload_scheduler.schedule_upload_simple(
                platform='alipan',  # 默认平台
                file_list=file_list,
                room_data=room_data,
                priority=UploadPriority.NORMAL
            )
    
    async def handle_bili_upload_event(self, event):
        """处理B站上传事件"""
        if getattr(event, 'room_id', None) is not None:
            return

        file_list = event.args[0] if event.args else []
        room_data = event.data.get('room_data', {})
        
        if file_list:
            await schedule_bili_submission(
                file_list=file_list,
                room_data=room_data,
                source='AUTO',
                priority=UploadPriority.HIGH  # B站上传优先级较高
            )


# 全局异步上传调度器实例
async_upload_scheduler = AsyncUploadScheduler(
    max_concurrent_uploads=4,
    max_concurrent_per_platform=2
)

# 上传事件处理器实例
upload_event_handler = UploadEventHandler(async_upload_scheduler)
