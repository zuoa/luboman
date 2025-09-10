import asyncio
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Callable, Optional, Any
from concurrent.futures import ThreadPoolExecutor
import weakref

logger = logging.getLogger('luboman')


@dataclass
class AsyncEvent:
    """异步事件对象"""
    type_: str  # 事件类型
    args: tuple = ()
    data: dict = field(default_factory=dict)  # 事件数据
    created_at: float = field(default_factory=time.time)
    priority: int = 0  # 优先级，数字越小优先级越高


class AsyncEventManager:
    """异步事件管理器 - 核心改造组件"""
    
    def __init__(self, worker_count: int = 4, queue_size: int = 5000):
        # 使用优先队列处理事件
        self.event_queue = asyncio.PriorityQueue(maxsize=queue_size)
        self.handlers: Dict[str, List[Callable]] = {}
        self.running = False
        self.worker_tasks: List[asyncio.Task] = []
        self.worker_count = worker_count
        
        # 性能统计
        self.stats = {
            'events_processed': 0,
            'events_failed': 0,
            'average_processing_time': 0.0,
            'queue_size': 0
        }
        
        # 线程池兜底（对于必须同步执行的任务）
        self.thread_pool = ThreadPoolExecutor(
            max_workers=4, 
            thread_name_prefix='AsyncEvent-Sync'
        )
        
        # 弱引用集合，避免内存泄漏
        self._cleanup_refs = weakref.WeakSet()
    
    async def start(self):
        """启动异步事件管理器"""
        if self.running:
            return
            
        self.running = True
        logger.info(f"启动异步事件管理器，工作进程数: {self.worker_count}")
        
        # 启动多个并发处理器
        for i in range(self.worker_count):
            task = asyncio.create_task(
                self._event_worker(f"async-worker-{i}"),
                name=f"async-worker-{i}"
            )
            self.worker_tasks.append(task)
        
        # 启动统计任务
        stats_task = asyncio.create_task(
            self._stats_reporter(),
            name="async-stats-reporter"
        )
        self.worker_tasks.append(stats_task)
        
        logger.info("异步事件管理器启动完成")
    
    async def stop(self):
        """停止异步事件管理器"""
        if not self.running:
            return
            
        logger.info("正在关闭异步事件管理器...")
        self.running = False
        
        # 等待所有工作任务完成
        if self.worker_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.worker_tasks, return_exceptions=True),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning("异步事件管理器关闭超时，强制终止")
                for task in self.worker_tasks:
                    if not task.done():
                        task.cancel()
        
        # 关闭线程池
        self.thread_pool.shutdown(wait=True, timeout=5)
        
        # 清理处理器
        self.handlers.clear()
        self.worker_tasks.clear()
        
        logger.info("异步事件管理器已关闭")
    
    async def _event_worker(self, worker_name: str):
        """事件处理工作器"""
        logger.debug(f"启动事件工作器: {worker_name}")
        
        while self.running:
            try:
                # 使用优先队列获取事件，带超时避免死锁
                try:
                    priority, event = await asyncio.wait_for(
                        self.event_queue.get(), 
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                start_time = time.time()
                
                # 处理事件
                await self._process_event_async(event, worker_name)
                
                # 更新统计
                processing_time = time.time() - start_time
                self.stats['events_processed'] += 1
                
                # 计算平均处理时间
                total_events = self.stats['events_processed']
                old_avg = self.stats['average_processing_time']
                self.stats['average_processing_time'] = (
                    (old_avg * (total_events - 1) + processing_time) / total_events
                )
                
                # 标记任务完成
                self.event_queue.task_done()
                
            except asyncio.CancelledError:
                logger.debug(f"事件工作器 {worker_name} 被取消")
                break
            except Exception as e:
                self.stats['events_failed'] += 1
                logger.error(f"事件工作器 {worker_name} 处理事件失败: {e}")
                logger.debug(f"错误详情: {traceback.format_exc()}")
    
    async def _process_event_async(self, event: AsyncEvent, worker_name: str):
        """异步处理单个事件"""
        if not self.running:
            return
            
        event_type = event.type_
        if event_type not in self.handlers:
            logger.debug(f"没有找到事件类型 {event_type} 的处理器")
            return
        
        # 并发执行所有处理器
        handler_tasks = []
        for handler in self.handlers[event_type]:
            if asyncio.iscoroutinefunction(handler):
                # 异步处理器
                task = asyncio.create_task(handler(event))
            else:
                # 同步处理器，在线程池中执行
                task = asyncio.create_task(
                    asyncio.get_event_loop().run_in_executor(
                        self.thread_pool, handler, event
                    )
                )
            handler_tasks.append(task)
        
        if handler_tasks:
            # 并发执行所有处理器，收集异常但不中断其他处理器
            results = await asyncio.gather(*handler_tasks, return_exceptions=True)
            
            # 处理结果和异常
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"事件处理器执行失败 ({worker_name}): {result}")
                elif result:
                    # 如果处理器返回了新事件，递归发送
                    if isinstance(result, AsyncEvent):
                        await self.send_event(result)
                    elif isinstance(result, (list, tuple)):
                        for sub_event in result:
                            if isinstance(sub_event, AsyncEvent):
                                await self.send_event(sub_event)
    
    async def send_event(self, event: AsyncEvent):
        """发送事件到异步队列"""
        if not self.running:
            logger.debug("事件管理器未运行，忽略事件")
            return
            
        try:
            # 使用优先队列，priority越小优先级越高
            await self.event_queue.put((event.priority, event))
            self.stats['queue_size'] = self.event_queue.qsize()
            
        except asyncio.QueueFull:
            # 队列满时，丢弃优先级最低的事件
            logger.warning("异步事件队列已满，尝试丢弃低优先级事件")
            
            # 尝试清理一些低优先级事件
            try:
                # 这是一个简化实现，实际可能需要更复杂的策略
                await asyncio.sleep(0.001)  # 短暂等待
                await self.event_queue.put((event.priority, event))
            except asyncio.QueueFull:
                self.stats['events_failed'] += 1
                logger.error(f"事件队列饱和，丢弃事件: {event.type_}")
    
    def register_handler(self, event_type: str, handler: Callable, priority: int = 0):
        """注册事件处理器"""
        if event_type not in self.handlers:
            self.handlers[event_type] = []
        
        # 按优先级插入
        handlers_list = self.handlers[event_type]
        inserted = False
        for i, existing_handler in enumerate(handlers_list):
            if hasattr(existing_handler, '_priority') and existing_handler._priority > priority:
                handlers_list.insert(i, handler)
                inserted = True
                break
        
        if not inserted:
            handlers_list.append(handler)
        
        # 设置处理器优先级属性
        handler._priority = priority
        
        logger.debug(f"注册事件处理器: {event_type} -> {handler.__name__}")
    
    def unregister_handler(self, event_type: str, handler: Callable):
        """注销事件处理器"""
        if event_type in self.handlers:
            try:
                self.handlers[event_type].remove(handler)
                if not self.handlers[event_type]:
                    del self.handlers[event_type]
            except ValueError:
                pass
    
    def register(self, event_type: str, priority: int = 0, handler_priority: int = 0):
        """装饰器：注册事件处理器"""
        def decorator(func):
            # 为处理器设置默认事件优先级
            async def wrapper(event):
                # 如果没有显式设置优先级，使用默认值
                if hasattr(event, 'priority') and event.priority == 0:
                    event.priority = priority
                return await func(event) if asyncio.iscoroutinefunction(func) else func(event)
            
            wrapper._priority = handler_priority
            wrapper.__name__ = func.__name__
            
            self.register_handler(event_type, wrapper, handler_priority)
            return wrapper
        
        return decorator
    
    async def _stats_reporter(self):
        """定期报告性能统计"""
        while self.running:
            try:
                await asyncio.sleep(30)  # 每30秒报告一次
                
                if self.stats['events_processed'] > 0:
                    logger.info(
                        f"异步事件统计 - "
                        f"已处理: {self.stats['events_processed']}, "
                        f"失败: {self.stats['events_failed']}, "
                        f"队列大小: {self.stats['queue_size']}, "
                        f"平均处理时间: {self.stats['average_processing_time']:.3f}s"
                    )
                    
                    # 性能警告
                    if self.stats['queue_size'] > 1000:
                        logger.warning(f"异步事件队列积压严重: {self.stats['queue_size']}")
                    
                    if self.stats['average_processing_time'] > 1.0:
                        logger.warning(f"事件处理平均耗时过高: {self.stats['average_processing_time']:.3f}s")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"统计报告失败: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取当前统计信息"""
        return {
            **self.stats,
            'queue_size': self.event_queue.qsize(),
            'worker_count': len(self.worker_tasks),
            'handlers_count': sum(len(handlers) for handlers in self.handlers.values())
        }


# 事件类型定义 - 保持与原有系统兼容
class AsyncEventType:
    """异步事件类型定义"""
    EVENT_CHECK_STATUS = "check-status"
    EVENT_DOWNLOAD_ASSET = "download-asset"
    EVENT_UPDATE_DB_ROOM_DATA = "update-db-room-data"
    EVENT_REFRESH_ROOM_INFO = "refresh-room-info"
    EVENT_PRE_RECORD = "pre-record"
    EVENT_RECORD = "record"
    EVENT_RECORD_COMPLETED = "record-completed"
    EVENT_NOTIFY = "notify-event"
    EVENT_UPLOAD_BILI = "upload-bili"
    EVENT_UPLOAD_BILI_COMPLETED = "upload-bili-completed"
    EVENT_UPLOAD = "upload"
    EVENT_UPLOAD_COMPLETED = "upload-completed"
    
    # 新增批量处理事件
    EVENT_BATCH_CHECK_STATUS = "batch-check-status"
    EVENT_BATCH_UPDATE_DB = "batch-update-db"
    EVENT_BATCH_DOWNLOAD_ASSETS = "batch-download-assets"


# 全局异步事件管理器实例
async_event_manager = AsyncEventManager(worker_count=6, queue_size=10000)


# 向后兼容适配器
class AsyncEventAdapter:
    """适配器：让旧的同步事件系统能够使用新的异步事件管理器"""
    
    def __init__(self, async_manager: AsyncEventManager):
        self.async_manager = async_manager
        self._loop = None
    
    def send(self, event):
        """同步发送事件的兼容接口"""
        if hasattr(event, 'type_'):
            async_event = AsyncEvent(event.type_, event.args, getattr(event, 'data', {}))
        else:
            # 兼容旧的事件格式
            async_event = AsyncEvent(str(event), (), {})
        
        # 在新线程中创建事件循环或使用现有循环
        try:
            loop = asyncio.get_running_loop()
            # 如果在异步上下文中，直接创建任务
            asyncio.create_task(self.async_manager.send_event(async_event))
        except RuntimeError:
            # 如果不在异步上下文中，使用call_soon_threadsafe
            if self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self.async_manager.send_event(async_event))
                )
    
    def set_loop(self, loop):
        """设置事件循环引用"""
        self._loop = loop
