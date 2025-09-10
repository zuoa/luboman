import asyncio
import logging
import time
import os
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import uuid

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


class AsyncUploadScheduler:
    """异步上传调度器 - 优化上传系统的调度机制"""
    
    def __init__(self, max_concurrent_uploads: int = 3, 
                 max_concurrent_per_platform: int = 2):
        self.max_concurrent_uploads = max_concurrent_uploads
        self.max_concurrent_per_platform = max_concurrent_per_platform
        
        # 上传队列（按优先级排序）
        self.upload_queue = asyncio.PriorityQueue(maxsize=1000)
        self.running = False
        
        # 工作器任务
        self.upload_workers: List[asyncio.Task] = []
        
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
        if not self.running:
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
        
        # 清理资源
        self.upload_workers.clear()
        self.active_uploads.clear()
        self.platform_semaphores.clear()
        
        logger.info("异步上传调度器已关闭")
    
    def _init_platform_semaphores(self):
        """初始化平台信号量"""
        platforms = ['biliweb', 'alipan', 'bdpan', 'telegram', 'local']
        
        for platform in platforms:
            self.platform_semaphores[platform] = asyncio.Semaphore(
                self.max_concurrent_per_platform
            )
    
    async def _upload_worker(self, worker_name: str):
        """上传工作器"""
        logger.debug(f"启动上传工作器: {worker_name}")
        
        while self.running:
            try:
                # 获取上传任务
                try:
                    priority_value, upload_task = await asyncio.wait_for(
                        self.upload_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                # 记录活跃任务
                self.active_uploads[upload_task.task_id] = upload_task
                self.stats['current_uploads'] += 1
                
                # 执行上传
                result = await self._execute_upload(upload_task, worker_name)
                
                # 处理结果
                await self._handle_upload_result(upload_task, result)
                
                # 清理活跃任务
                self.active_uploads.pop(upload_task.task_id, None)
                self.stats['current_uploads'] -= 1
                
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
                success, uploaded_files, failed_files, error_msg = await self._perform_upload(
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
                    upload_speed=upload_speed
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
            total_size = await asyncio.get_event_loop().run_in_executor(
                None, calculate_size_sync
            )
        except Exception as e:
            logger.warning(f"计算文件大小失败: {e}")
        
        return total_size
    
    async def _perform_upload(self, upload_task: AsyncUploadTask) -> Tuple[bool, List[str], List[str], Optional[str]]:
        """执行实际上传操作"""
        platform = upload_task.platform
        file_list = upload_task.file_list
        
        uploaded_files = []
        failed_files = []
        error_message = None
        
        try:
            # 导入上传函数
            from luboman.core.upload import upload
            
            # 准备上传参数
            upload_kwargs = {}
            if upload_task.room_data:
                upload_kwargs.update(upload_task.room_data)
            
            # 在线程中执行上传（因为原有上传系统是同步的）
            def upload_sync():
                return upload(platform, file_list, **upload_kwargs)
            
            result = await asyncio.get_event_loop().run_in_executor(
                None, upload_sync
            )
            
            # 处理上传结果
            if result:
                uploaded_files = [f.get('video', '') for f in file_list]
                return True, uploaded_files, [], None
            else:
                failed_files = [f.get('video', '') for f in file_list]
                return False, [], failed_files, "上传返回失败结果"
        
        except Exception as e:
            error_message = str(e)
            failed_files = [f.get('video', '') for f in file_list]
            return False, [], failed_files, error_message
    
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
                
                # 延迟后重新加入队列
                asyncio.create_task(self._schedule_retry(upload_task, retry_delay))
        
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
    
    async def schedule_upload(self, upload_task: AsyncUploadTask):
        """调度上传任务"""
        if not self.running:
            logger.warning("上传调度器未运行，忽略上传任务")
            return
        
        try:
            # 使用优先级值作为队列排序键
            priority_value = upload_task.priority.value
            
            await self.upload_queue.put((priority_value, upload_task))
            self.stats['queue_size'] = self.upload_queue.qsize()
            
            logger.debug(
                f"上传任务已加入队列 - "
                f"平台: {upload_task.platform}, "
                f"优先级: {upload_task.priority.name}, "
                f"队列大小: {self.stats['queue_size']}"
            )
            
        except asyncio.QueueFull:
            logger.error(f"上传队列已满，丢弃任务: {upload_task.task_id}")
    
    async def schedule_upload_simple(self, platform: str, file_list: List[Dict[str, Any]], 
                                   room_data: Optional[Dict[str, Any]] = None,
                                   priority: UploadPriority = UploadPriority.NORMAL):
        """简化的上传调度接口"""
        upload_task = AsyncUploadTask(
            platform=platform,
            file_list=file_list,
            room_data=room_data or {},
            priority=priority
        )
        
        await self.schedule_upload(upload_task)
        return upload_task.task_id
    
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
                
                priority_value, upload_task = item
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


# 上传事件处理器
class UploadEventHandler:
    """上传事件处理器 - 集成到异步事件系统"""
    
    def __init__(self, upload_scheduler: AsyncUploadScheduler):
        self.upload_scheduler = upload_scheduler
    
    async def handle_upload_event(self, event):
        """处理上传事件"""
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
        file_list = event.args[0] if event.args else []
        room_data = event.data.get('room_data', {})
        
        if file_list:
            await self.upload_scheduler.schedule_upload_simple(
                platform='biliweb',
                file_list=file_list,
                room_data=room_data,
                priority=UploadPriority.HIGH  # B站上传优先级较高
            )


# 全局异步上传调度器实例
async_upload_scheduler = AsyncUploadScheduler(
    max_concurrent_uploads=4,
    max_concurrent_per_platform=2
)

# 上传事件处理器实例
upload_event_handler = UploadEventHandler(async_upload_scheduler)
