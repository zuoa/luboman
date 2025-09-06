import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

logger = logging.getLogger('luboman')

class GlobalThreadPoolManager:
    """全局线程池管理器"""
    _instance: Optional['GlobalThreadPoolManager'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        
        self._initialized = True
        self._pools: Dict[str, ThreadPoolExecutor] = {}
        self._shutdown = False
        
        # 根据系统CPU核心数动态调整线程池大小
        import os
        cpu_count = os.cpu_count() or 4
        
        self._pools = {
            'NORMAL': ThreadPoolExecutor(
                max_workers=max(2, cpu_count // 2), 
                thread_name_prefix='Global-NORMAL'
            ),
            'SLOW': ThreadPoolExecutor(
                max_workers=max(3, cpu_count), 
                thread_name_prefix='Global-SLOW'
            ),
        }
        logger.info(f"初始化全局线程池: NORMAL={max(2, cpu_count // 2)}, SLOW={max(3, cpu_count)}")
    
    def get_pool(self, pool_type: str) -> Optional[ThreadPoolExecutor]:
        """获取指定类型的线程池"""
        if self._shutdown:
            logger.warning(f"线程池管理器已关闭，无法获取 {pool_type} 线程池")
            return None
        return self._pools.get(pool_type)
    
    def submit_task(self, pool_type: str, fn, *args, **kwargs):
        """提交任务到指定线程池"""
        pool = self.get_pool(pool_type)
        if pool:
            try:
                return pool.submit(fn, *args, **kwargs)
            except Exception as e:
                logger.error(f"提交任务到线程池 {pool_type} 失败: {e}")
                return None
        else:
            logger.error(f"无法获取线程池 {pool_type}")
            return None
    
    def shutdown(self, wait: bool = True, timeout: float = 30.0):
        """关闭所有线程池"""
        if self._shutdown:
            return
            
        logger.info("开始关闭全局线程池...")
        self._shutdown = True
        
        for pool_name, pool in self._pools.items():
            try:
                logger.info(f"关闭线程池: {pool_name}")
                pool.shutdown(wait=False)  # 先发送关闭信号
            except Exception as e:
                logger.error(f"关闭线程池 {pool_name} 时出错: {e}")
        
        if wait:
            # 等待所有线程池完成，带超时
            start_time = time.time()
            for pool_name, pool in self._pools.items():
                remaining_time = timeout - (time.time() - start_time)
                if remaining_time <= 0:
                    logger.warning(f"等待线程池 {pool_name} 关闭超时")
                    break
                try:
                    # 等待剩余任务完成
                    if not hasattr(pool, '_threads') or not pool._threads:  # 如果没有活跃线程，跳过等待
                        continue
                    pool.shutdown(wait=True)
                    logger.info(f"线程池 {pool_name} 已安全关闭")
                except Exception as e:
                    logger.error(f"等待线程池 {pool_name} 关闭时出错: {e}")
    
    def get_stats(self) -> Dict:
        """获取线程池统计信息"""
        stats = {}
        for pool_name, pool in self._pools.items():
            stats[pool_name] = {
                'max_workers': pool._max_workers,
                'threads': len(pool._threads) if hasattr(pool, '_threads') else 0,
                'pending_tasks': pool._work_queue.qsize() if hasattr(pool, '_work_queue') else 0
            }
        return stats
    
    def is_healthy(self) -> bool:
        """检查线程池是否健康"""
        if self._shutdown:
            return False
        
        try:
            for pool_name, pool in self._pools.items():
                # 检查线程池是否响应
                future = pool.submit(lambda: True)
                result = future.result(timeout=1.0)  # 1秒超时
                if not result:
                    logger.warning(f"线程池 {pool_name} 健康检查失败")
                    return False
            return True
        except Exception as e:
            logger.error(f"线程池健康检查出错: {e}")
            return False

# 全局实例
thread_pool_manager = GlobalThreadPoolManager()
