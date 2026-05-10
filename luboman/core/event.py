import atexit
import functools
import logging
import weakref
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from queue import Queue
from threading import Thread, Lock
from typing import Generator, Set

logger = logging.getLogger('luboman')


class EventManager(Thread):
    def __init__(self):
        super().__init__(name='Synchronous', daemon=True)

        # 事件队列，设置最大大小防止内存泄漏
        self.__queue = Queue(maxsize=1000)

        # 事件引擎开关
        self.__active = True

        self.__handlers = {}
        self.__pool_blocks = []
        
        # Future 对象追踪 - 修复内存泄漏问题
        self.__active_futures: Set[Future] = set()
        self.__futures_lock = Lock()
        self.__futures_cleanup_counter = 0
        
        # 尝试使用全局线程池
        self.__use_global_pool = True
        self.__thread_pool = None
        
        try:
            from .thread_pool import thread_pool_manager
            self._global_manager = thread_pool_manager
            if not thread_pool_manager.get_pool('NORMAL'):
                raise Exception("全局线程池不可用")
            logger.info("EventManager 使用全局线程池")
        except Exception as e:
            logger.warning(f"全局线程池不可用，创建本地线程池: {e}")
            self.__use_global_pool = False
            self._global_manager = None
            self.__thread_pool = {
                'NORMAL': ThreadPoolExecutor(2, thread_name_prefix='Local-NORMAL'),
                'SLOW': ThreadPoolExecutor(3, thread_name_prefix='Local-SLOW'),
            }
            # 注册退出时的清理函数
            atexit.register(self._cleanup_local_pools)

    def _cleanup_local_pools(self):
        """清理本地线程池"""
        if self.__thread_pool:
            for pool in self.__thread_pool.values():
                try:
                    pool.shutdown(wait=True)
                except Exception as e:
                    logger.error(f"清理本地线程池时出错: {e}")
    
    def _future_done_callback(self, future: Future):
        """Future 完成时的回调函数，用于自动清理"""
        try:
            # 从活跃 Future 集合中移除
            with self.__futures_lock:
                self.__active_futures.discard(future)
            
            # 获取异常（如果有的话），避免异常被吞没
            if future.exception() is not None:
                exc = future.exception()
                logger.error(f"事件处理任务执行时发生异常: {exc}", exc_info=True)
        except Exception as e:
            logger.error(f"清理 Future 对象时出错: {e}")
    
    def _cleanup_completed_futures(self):
        """定期清理已完成的 Future 对象"""
        try:
            with self.__futures_lock:
                # 过滤出已完成的 Future
                completed = {f for f in self.__active_futures if f.done()}
                if completed:
                    self.__active_futures -= completed
                    logger.debug(f"清理了 {len(completed)} 个已完成的 Future 对象")
        except Exception as e:
            logger.error(f"清理已完成的 Future 时出错: {e}")
    
    def _wait_for_futures(self, timeout: float = 30.0):
        """等待所有活跃的 Future 完成"""
        import time
        from concurrent.futures import wait, FIRST_COMPLETED
        
        try:
            with self.__futures_lock:
                active_count = len(self.__active_futures)
                if active_count == 0:
                    return
                
                logger.info(f"等待 {active_count} 个活跃任务完成...")
                futures_to_wait = set(self.__active_futures)
            
            # 等待所有 Future 完成或超时
            start_time = time.time()
            while futures_to_wait and (time.time() - start_time) < timeout:
                # 等待一批完成
                done, futures_to_wait = wait(futures_to_wait, timeout=1.0, return_when=FIRST_COMPLETED)
                
                if done:
                    with self.__futures_lock:
                        self.__active_futures -= done
                    logger.debug(f"已完成 {len(done)} 个任务，剩余 {len(futures_to_wait)} 个")
            
            # 如果超时还有未完成的任务
            if futures_to_wait:
                logger.warning(f"等待超时，仍有 {len(futures_to_wait)} 个任务未完成")
                # 强制清理
                with self.__futures_lock:
                    self.__active_futures.clear()
        except Exception as e:
            logger.error(f"等待 Future 完成时出错: {e}")

    def stop(self):
        """停止事件管理器"""
        logger.debug(f"停止EventManager: {self.name}")
        self.__active = False
        
        # 等待所有活跃的 Future 完成
        self._wait_for_futures(timeout=30.0)
        
        # 清理事件处理器，防止循环引用
        self.__handlers.clear()
        self.__pool_blocks.clear()
        
        # 如果使用本地线程池，需要关闭
        if not self.__use_global_pool and self.__thread_pool:
            for pool_name, pool in self.__thread_pool.items():
                try:
                    logger.debug(f"关闭本地线程池: {pool_name}")
                    pool.shutdown(wait=True)
                except Exception as e:
                    logger.error(f"关闭本地线程池 {pool_name} 失败: {e}")
        
        # 清空队列中剩余的事件
        try:
            while not self.__queue.empty():
                self.__queue.get_nowait()
        except Exception:
            pass
        
        # 最终清理 Future 集合
        with self.__futures_lock:
            self.__active_futures.clear()
            logger.debug("EventManager 停止完成")

    def run(self):
        while self.__active:
            try:
                event = self.__queue.get(block=True, timeout=1)
                self.__event_process(event)
                
                # 每处理 100 个事件，清理一次已完成的 Future
                self.__futures_cleanup_counter += 1
                if self.__futures_cleanup_counter >= 100:
                    self._cleanup_completed_futures()
                    self.__futures_cleanup_counter = 0
            except Exception as e:
                pass

    def __event_process(self, event):
        if not self.__active:
            return
            
        if event and event.type_ in self.__handlers:
            for handler in self.__handlers[event.type_]:
                if handler.__qualname__ in self.__pool_blocks:
                    # 使用全局线程池或本地线程池
                    future = None
                    if self.__use_global_pool and self._global_manager:
                        future = self._global_manager.submit_task(
                            handler.thread_pool, handler, event
                        )
                        if future is None:
                            logger.error(f"提交任务到全局线程池失败: {handler.__qualname__}")
                    else:
                        pool = self.__thread_pool.get(handler.thread_pool) if self.__thread_pool else None
                        if pool:
                            try:
                                future = pool.submit(handler, event)
                            except Exception as e:
                                logger.error(f"提交任务到本地线程池失败: {e}")
                        else:
                            logger.error(f"无法获取线程池: {handler.thread_pool}")
                    
                    # 追踪 Future 对象并添加完成回调
                    if future is not None:
                        with self.__futures_lock:
                            self.__active_futures.add(future)
                        # 添加完成回调自动清理
                        future.add_done_callback(self._future_done_callback)
                else:
                    try:
                        handler(event)
                    except Exception as e:
                        logger.error(f"处理事件时出错: {e}")

    def get_stats(self):
        """获取事件管理器统计信息"""
        with self.__futures_lock:
            active_futures_count = len(self.__active_futures)
            completed_futures = sum(1 for f in self.__active_futures if f.done())
        
        return {
            'active': self.__active,
            'queue_size': self.__queue.qsize(),
            'handlers_count': sum(len(handlers) for handlers in self.__handlers.values()),
            'active_futures': active_futures_count,
            'completed_futures': completed_futures,
            'pending_futures': active_futures_count - completed_futures,
            'use_global_pool': self.__use_global_pool
        }
    
    def add_event_handler(self, type_, handler):
        if type_ not in self.__handlers:
            self.__handlers[type_] = []
        self.__handlers[type_].append(handler)

    def remove_event_handler(self, type_, handler):
        if type_ in self.__handlers:
            self.__handlers[type_].remove(handler)

    def fire(self, type_, *args, **kwargs):
        if type_ in self.__handlers:
            for handler in self.__handlers[type_]:
                handler(*args, **kwargs)

    def send(self, event):
        """发送事件，向事件队列中存入事件"""
        if not self.__active:
            return
            
        try:
            # 使用非阻塞方式，避免队列满时卡死
            self.__queue.put_nowait(event)
        except Exception:
            # 队列满时，根据事件重要性决定处理策略
            current_size = self.__queue.qsize()
            logger.warning(f"事件队列已满 (大小: {current_size})")
            
            # 高优先级事件：状态检查、录制相关，需要保留
            high_priority_events = [
                EventType.EVENT_CHECK_STATUS,
                EventType.EVENT_RECORD,
                EventType.EVENT_PRE_RECORD,
                EventType.EVENT_RECORD_COMPLETED
            ]
            
            # 低优先级事件：上传、通知等可以丢弃
            low_priority_events = [
                EventType.EVENT_UPLOAD,
                EventType.EVENT_UPLOAD_BILI,
                EventType.EVENT_NOTIFY,
                EventType.EVENT_DOWNLOAD_ASSET
            ]
            
            if event.type_ in high_priority_events:
                # 高优先级事件：尝试清理低优先级事件为其腾出空间
                try:
                    cleared_count = 0
                    temp_events = []
                    
                    # 从队列中取出所有事件
                    while not self.__queue.empty() and cleared_count < 10:
                        try:
                            old_event = self.__queue.get_nowait()
                            if old_event.type_ not in low_priority_events:
                                temp_events.append(old_event)
                            else:
                                cleared_count += 1
                        except Exception:
                            break
                    
                    # 将保留的事件重新放回队列
                    for temp_event in temp_events:
                        try:
                            self.__queue.put_nowait(temp_event)
                        except Exception:
                            break
                    
                    # 尝试添加新的高优先级事件
                    self.__queue.put_nowait(event)
                    if cleared_count > 0:
                        logger.info(f"为高优先级事件清理了 {cleared_count} 个低优先级事件")
                        
                except Exception as e:
                    logger.error(f"清理队列失败: {e}")
                    
            elif event.type_ in low_priority_events:
                # 低优先级事件：直接丢弃
                logger.debug(f"队列满，丢弃低优先级事件: {event.type_}")
            else:
                # 中等优先级事件：尝试简单的FIFO清理
                try:
                    self.__queue.get_nowait()  # 移除最旧事件
                    self.__queue.put_nowait(event)  # 添加新事件
                    logger.debug(f"队列满，使用FIFO策略处理事件: {event.type_}")
                except Exception:
                    logger.error("无法处理队列满的情况")

    def register(self, type_, block="NORMAL"):
        def callback(result):
            if not result:
                pass
            elif isinstance(result, (tuple, Generator)):
                for event in result:
                    self.send(event)
            else:
                self.send(result)

        def block_append(fc, blk):
            if blk:
                self.__pool_blocks.append(fc.__qualname__)

        def decorator(func):
            block_append(func, block)

            @functools.wraps(func)
            def wrapper(event):
                _event = func(*event.args)
                callback(_event)
                return _event

            wrapper.thread_pool = block
            self.add_event_handler(type_, wrapper)
            return wrapper

        return decorator


class EventType:
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


@dataclass
class Event:
    """事件对象"""
    type_: str  # 事件类型
    args: tuple = ()
    data: dict = field(default_factory=dict)  # 字典用于保存具体的事件数据


if __name__ == '__main__':
    import inspect

    print(inspect.getouterframes(inspect.currentframe())[1][3])
