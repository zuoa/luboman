import asyncio
import inspect
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Callable, Optional, Any
from concurrent.futures import ThreadPoolExecutor
import itertools

from luboman.core.async_utils import run_blocking

logger = logging.getLogger('luboman')

# 全局序列号生成器，确保事件的唯一性
_event_sequence = itertools.count()


@dataclass(slots=True)
class AsyncEvent:
    """异步事件对象"""
    type_: str  # 事件类型
    args: tuple = ()
    data: dict = field(default_factory=dict)  # 事件数据
    room_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    priority: int = 0  # 优先级，数字越小优先级越高
    sequence: int = field(default_factory=lambda: next(_event_sequence))  # 序列号，用于比较

    def __post_init__(self):
        if self.room_id is None and isinstance(self.data, dict):
            room_id = self.data.get('room_id')
            if room_id is not None:
                self.room_id = str(room_id)
        elif self.room_id is not None and isinstance(self.data, dict):
            self.room_id = str(self.room_id)
            self.data.setdefault('room_id', self.room_id)
    
    def __lt__(self, other):
        """定义小于比较，首先按优先级，然后按序列号"""
        if not isinstance(other, AsyncEvent):
            return NotImplemented
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.sequence < other.sequence
    
    def __le__(self, other):
        """定义小于等于比较"""
        if not isinstance(other, AsyncEvent):
            return NotImplemented
        return self < other or self == other
    
    def __gt__(self, other):
        """定义大于比较"""
        if not isinstance(other, AsyncEvent):
            return NotImplemented
        return not self <= other
    
    def __ge__(self, other):
        """定义大于等于比较"""
        if not isinstance(other, AsyncEvent):
            return NotImplemented
        return not self < other
    
    def __eq__(self, other):
        """定义相等比较，基于序列号（每个事件都是唯一的）"""
        if not isinstance(other, AsyncEvent):
            return NotImplemented
        return self.sequence == other.sequence
    
    def __hash__(self):
        """定义哈希值，基于序列号"""
        return hash(self.sequence)


class AsyncEventManager:
    """异步事件管理器 - 核心改造组件"""
    
    def __init__(self, worker_count: int = 4, queue_size: int = 5000):
        # 使用优先队列处理事件
        self.event_queue = asyncio.PriorityQueue(maxsize=queue_size)
        self.handlers: Dict[str, List[Callable]] = {}
        self.running = False
        self.worker_tasks: List[asyncio.Task] = []
        self.stats_task: Optional[asyncio.Task] = None
        self.worker_count = worker_count
        
        # 性能统计
        self.stats = {
            'events_processed': 0,
            'events_failed': 0,
            'events_dropped': 0,
            'average_processing_time': 0.0,
            'queue_size': 0
        }
        
        # 线程池兜底（对于必须同步执行的任务）
        self.thread_pool = ThreadPoolExecutor(
            max_workers=4, 
            thread_name_prefix='AsyncEvent-Sync'
        )
        self._thread_pool_shutdown = False
        
    async def start(self):
        """启动异步事件管理器"""
        if self.running:
            return

        if self._thread_pool_shutdown:
            self.thread_pool = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix='AsyncEvent-Sync'
            )
            self._thread_pool_shutdown = False
            
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
        self.stats_task = asyncio.create_task(
            self._stats_reporter(),
            name="async-stats-reporter"
        )
        
        logger.info("异步事件管理器启动完成")
    
    async def stop(self):
        """停止异步事件管理器"""
        if not self.running:
            return
            
        logger.info("正在关闭异步事件管理器...")
        self.running = False
        
        if self.stats_task and not self.stats_task.done():
            self.stats_task.cancel()

        tasks = list(self.worker_tasks)
        if self.stats_task:
            tasks.append(self.stats_task)

        # 等待工作器完成当前事件，统计任务直接取消，避免关闭固定等待5分钟睡眠。
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("异步事件管理器关闭超时，强制终止")
                for task in tasks:
                    if not task.done():
                        task.cancel()

        self._drain_event_queue()
        
        # 关闭线程池 - 兼容旧版本Python
        try:
            # Python 3.9+ 支持timeout参数
            self.thread_pool.shutdown(wait=True, timeout=5)
        except TypeError:
            # Python 3.8及以下版本不支持timeout参数
            self.thread_pool.shutdown(wait=True)
        self._thread_pool_shutdown = True
        
        # 清理处理器
        self.handlers.clear()
        self.worker_tasks.clear()
        self.stats_task = None
        
        logger.info("异步事件管理器已关闭")

    def _drain_event_queue(self):
        """释放关闭时仍留在队列中的事件引用。"""
        drained = 0
        while True:
            try:
                self.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.event_queue.task_done()
            drained += 1

        self.stats['queue_size'] = self.event_queue.qsize()
        if drained:
            logger.info(f"已清理 {drained} 个未处理的异步事件")
    
    async def _event_worker(self, worker_name: str):
        """事件处理工作器"""
        # 移除工作器启动debug日志 - 工作器启动会在manager启动时统一报告
        
        while self.running:
            try:
                event = None
                try:
                    event = await asyncio.wait_for(
                        self.event_queue.get(), 
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                start_time = time.time()
                
                try:
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
                finally:
                    self.event_queue.task_done()
                
            except asyncio.CancelledError:
                # 工作器取消是正常关闭流程，不需要debug日志
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
        handlers = self.handlers.get(event_type)
        if not handlers:
            # 精简频繁debug日志：未注册的事件类型可能较多，降级为仅在必要时记录
            return
        
        # 并发执行所有处理器
        handler_tasks = []
        event_room_id = event.room_id
        for handler in tuple(handlers):
            handler_room_id = getattr(handler, '_room_id', None)
            if handler_room_id is not None and handler_room_id != event_room_id:
                continue

            handler_tasks.append(asyncio.create_task(self._run_handler(handler, event)))
        
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

    async def _run_handler(self, handler: Callable, event: AsyncEvent):
        """运行单个事件处理器，统一支持同步、异步和返回 awaitable 的处理器。"""
        if asyncio.iscoroutinefunction(handler):
            return await handler(event)

        result = await run_blocking(handler, event, executor=self.thread_pool)
        if inspect.isawaitable(result):
            return await result
        return result
    
    async def send_event(self, event: AsyncEvent):
        """发送事件到异步队列"""
        if not self.running:
            # 管理器未运行时忽略事件是正常情况，不需要频繁debug日志
            return
            
        try:
            # 非阻塞入队，避免高负载下发送方被无限挂起并放大内存占用。
            self.event_queue.put_nowait(event)
            self.stats['queue_size'] = self.event_queue.qsize()
            
        except asyncio.QueueFull:
            # 高优先级事件短暂等待一次；低优先级事件直接丢弃，保护常驻内存。
            if event.priority <= 1:
                try:
                    await asyncio.wait_for(self.event_queue.put(event), timeout=0.1)
                    self.stats['queue_size'] = self.event_queue.qsize()
                    return
                except (asyncio.QueueFull, asyncio.TimeoutError):
                    pass

            self.stats['events_failed'] += 1
            self.stats['events_dropped'] += 1
            logger.warning(f"异步事件队列饱和，丢弃事件: {event.type_}")
    
    def register_handler(self, event_type: str, handler: Callable, priority: int = 0,
                         room_id: Optional[str] = None):
        """注册事件处理器"""
        if event_type not in self.handlers:
            self.handlers[event_type] = []

        scoped_room_id = str(room_id) if room_id is not None else None
        
        # 检查是否是bound method，如果是则包装它
        if hasattr(handler, '__self__') and hasattr(handler, '__func__'):
            # 这是一个bound method，需要包装
            original_handler = handler
            
            if asyncio.iscoroutinefunction(handler):
                async def wrapper(*args, **kwargs):
                    return await original_handler(*args, **kwargs)
            else:
                def wrapper(*args, **kwargs):
                    return original_handler(*args, **kwargs)
            
            wrapper.__name__ = getattr(handler, '__name__', str(handler))
            wrapper._priority = priority
            wrapper._room_id = scoped_room_id
            wrapper._original_handler = original_handler
            actual_handler = wrapper
        else:
            # 普通函数或已经可以设置属性的对象
            try:
                handler._priority = priority
                handler._room_id = scoped_room_id
                actual_handler = handler
            except AttributeError:
                # 如果仍然无法设置属性，也包装一下
                if asyncio.iscoroutinefunction(handler):
                    async def wrapper(*args, **kwargs):
                        return await handler(*args, **kwargs)
                else:
                    def wrapper(*args, **kwargs):
                        return handler(*args, **kwargs)
                
                wrapper.__name__ = getattr(handler, '__name__', str(handler))
                wrapper._priority = priority
                wrapper._room_id = scoped_room_id
                wrapper._original_handler = handler
                actual_handler = wrapper
        
        # 按优先级插入
        handlers_list = self.handlers[event_type]
        inserted = False
        for i, existing_handler in enumerate(handlers_list):
            existing_priority = getattr(existing_handler, '_priority', 0)
            if existing_priority > priority:
                handlers_list.insert(i, actual_handler)
                inserted = True
                break
        
        if not inserted:
            handlers_list.append(actual_handler)
        
        # 精简启动时频繁的debug日志：处理器注册在info级别已有总体报告
        return actual_handler
    
    def unregister_handler(self, event_type: str, handler: Callable):
        """注销事件处理器"""
        if event_type in self.handlers:
            # 首先尝试直接移除
            try:
                self.handlers[event_type].remove(handler)
                if not self.handlers[event_type]:
                    del self.handlers[event_type]
                return
            except ValueError:
                pass
            
            # 如果直接移除失败，检查是否是包装的处理器
            handlers_to_remove = []
            for registered_handler in self.handlers[event_type]:
                if hasattr(registered_handler, '_original_handler'):
                    if registered_handler._original_handler == handler:
                        handlers_to_remove.append(registered_handler)
            
            # 移除找到的包装处理器
            for wrapped_handler in handlers_to_remove:
                try:
                    self.handlers[event_type].remove(wrapped_handler)
                except ValueError:
                    pass
            
            # 如果列表为空，删除该事件类型
            if not self.handlers[event_type]:
                del self.handlers[event_type]
    
    def register(self, event_type: str, priority: int = 0,
                 handler_priority: Optional[int] = None,
                 room_id: Optional[str] = None):
        """装饰器：注册事件处理器"""
        actual_priority = priority if handler_priority is None else handler_priority

        def decorator(func):
            return self.register_handler(
                event_type,
                func,
                priority=actual_priority,
                room_id=room_id
            )
        
        return decorator
    
    async def _stats_reporter(self):
        """定期报告性能统计"""
        while self.running:
            try:
                await asyncio.sleep(300)  # 每5分钟报告一次，减少日志频率
                
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
            # 保留原有优先级设置
            priority = getattr(event, 'priority', 0)
            async_event = AsyncEvent(
                event.type_, 
                event.args, 
                getattr(event, 'data', {}),
                room_id=getattr(event, 'room_id', None),
                priority=priority
            )
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
