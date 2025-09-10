import asyncio
import logging
import time
import sqlite3
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
import weakref
import json
from datetime import datetime

logger = logging.getLogger('luboman')


@dataclass
class DatabaseOperation:
    """数据库操作数据结构"""
    operation_type: str  # 'update', 'insert', 'delete', 'select'
    table: str
    data: Dict[str, Any]
    where_clause: Optional[Dict[str, Any]] = None
    room_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    priority: int = 0


@dataclass
class DatabaseResult:
    """数据库操作结果"""
    success: bool
    operation_id: Optional[str] = None
    affected_rows: int = 0
    error: Optional[str] = None
    execution_time: float = 0.0
    data: Optional[Any] = None


class AsyncDatabaseManager:
    """异步数据库管理器 - 批量处理数据库操作，减少连接开销"""
    
    def __init__(self, batch_size: int = 50, batch_timeout: float = 5.0):
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        
        # 操作队列
        self.operation_queue = asyncio.Queue(maxsize=2000)
        self.running = False
        self.batch_processor_task: Optional[asyncio.Task] = None
        
        # 批量操作缓冲区
        self.pending_operations: List[DatabaseOperation] = []
        self.last_batch_time = time.time()
        
        # 性能统计
        self.stats = {
            'operations_total': 0,
            'operations_success': 0,
            'operations_failed': 0,
            'batches_processed': 0,
            'average_batch_size': 0.0,
            'average_execution_time': 0.0
        }
        
        # 连接池管理（简化版，实际使用中应该使用专业的连接池）
        self._connection_lock = asyncio.Lock()
        
    async def start(self):
        """启动异步数据库管理器"""
        if self.running:
            return
            
        self.running = True
        logger.info("启动异步数据库管理器")
        
        # 启动批量处理器
        self.batch_processor_task = asyncio.create_task(
            self._batch_processor(),
            name="database-batch-processor"
        )
        
        logger.info("异步数据库管理器启动完成")
    
    async def stop(self):
        """停止异步数据库管理器"""
        if not self.running:
            return
            
        logger.info("正在关闭异步数据库管理器...")
        self.running = False
        
        # 处理剩余的操作
        if self.pending_operations:
            logger.info(f"处理剩余的 {len(self.pending_operations)} 个数据库操作")
            await self._execute_batch(self.pending_operations)
            self.pending_operations.clear()
        
        # 停止批量处理器
        if self.batch_processor_task and not self.batch_processor_task.done():
            self.batch_processor_task.cancel()
            try:
                await self.batch_processor_task
            except asyncio.CancelledError:
                pass
        
        logger.info("异步数据库管理器已关闭")
    
    async def _batch_processor(self):
        """批量处理器 - 核心批量处理逻辑"""
        logger.debug("启动数据库批量处理器")
        
        while self.running:
            try:
                # 等待新操作或超时
                try:
                    operation = await asyncio.wait_for(
                        self.operation_queue.get(),
                        timeout=1.0
                    )
                    self.pending_operations.append(operation)
                except asyncio.TimeoutError:
                    # 超时，检查是否需要执行批量操作
                    pass
                
                # 检查是否需要执行批量操作
                current_time = time.time()
                should_execute_batch = (
                    len(self.pending_operations) >= self.batch_size or
                    (self.pending_operations and 
                     current_time - self.last_batch_time >= self.batch_timeout)
                )
                
                if should_execute_batch and self.pending_operations:
                    batch = self.pending_operations.copy()
                    self.pending_operations.clear()
                    self.last_batch_time = current_time
                    
                    # 异步执行批量操作
                    await self._execute_batch(batch)
                    
            except asyncio.CancelledError:
                logger.debug("数据库批量处理器被取消")
                break
            except Exception as e:
                logger.error(f"数据库批量处理器错误: {e}")
                await asyncio.sleep(1)  # 错误后等待1秒
    
    async def _execute_batch(self, operations: List[DatabaseOperation]):
        """执行批量数据库操作"""
        if not operations:
            return
        
        start_time = time.time()
        success_count = 0
        
        try:
            # 按操作类型分组以优化执行
            grouped_operations = self._group_operations(operations)
            
            async with self._connection_lock:
                # 在实际应用中，这里应该使用异步数据库驱动
                # 目前使用同步操作在线程中执行
                results = await asyncio.get_event_loop().run_in_executor(
                    None, self._execute_batch_sync, grouped_operations
                )
                
                success_count = sum(1 for result in results if result.success)
        
        except Exception as e:
            logger.error(f"批量数据库操作失败: {e}")
            results = [DatabaseResult(
                success=False,
                error=str(e),
                execution_time=time.time() - start_time
            ) for _ in operations]
        
        # 更新统计
        execution_time = time.time() - start_time
        self.stats['operations_total'] += len(operations)
        self.stats['operations_success'] += success_count
        self.stats['operations_failed'] += len(operations) - success_count
        self.stats['batches_processed'] += 1
        
        # 更新平均值
        total_batches = self.stats['batches_processed']
        old_avg_size = self.stats['average_batch_size']
        self.stats['average_batch_size'] = (
            (old_avg_size * (total_batches - 1) + len(operations)) / total_batches
        )
        
        old_avg_time = self.stats['average_execution_time']
        self.stats['average_execution_time'] = (
            (old_avg_time * (total_batches - 1) + execution_time) / total_batches
        )
        
        logger.info(
            f"批量数据库操作完成 - "
            f"操作数: {len(operations)}, "
            f"成功: {success_count}, "
            f"耗时: {execution_time:.3f}s"
        )
    
    def _group_operations(self, operations: List[DatabaseOperation]) -> Dict[str, List[DatabaseOperation]]:
        """按操作类型和表分组操作以优化执行"""
        groups = {}
        
        for op in operations:
            key = f"{op.operation_type}:{op.table}"
            if key not in groups:
                groups[key] = []
            groups[key].append(op)
        
        return groups
    
    def _execute_batch_sync(self, grouped_operations: Dict[str, List[DatabaseOperation]]) -> List[DatabaseResult]:
        """同步执行批量数据库操作（在线程中运行）"""
        results = []
        
        try:
            # 这里应该使用实际的数据库连接
            # 现在使用模拟实现
            from luboman.database.db import DB
            
            for group_key, operations in grouped_operations.items():
                operation_type, table = group_key.split(':', 1)
                
                if operation_type == 'update' and table == 'live_room':
                    # 批量更新直播间数据
                    results.extend(self._batch_update_live_rooms(operations))
                elif operation_type == 'insert' and table == 'record_file':
                    # 批量插入录制文件记录
                    results.extend(self._batch_insert_record_files(operations))
                else:
                    # 其他操作类型
                    results.extend(self._execute_individual_operations(operations))
        
        except Exception as e:
            logger.error(f"同步批量操作失败: {e}")
            results = [DatabaseResult(
                success=False,
                error=str(e)
            ) for _ in sum(grouped_operations.values(), [])]
        
        return results
    
    def _batch_update_live_rooms(self, operations: List[DatabaseOperation]) -> List[DatabaseResult]:
        """批量更新直播间数据"""
        results = []
        
        try:
            from luboman.database.db import DB
            
            # 构造批量更新语句
            updates = []
            for op in operations:
                room_data = op.data
                if 'id' in room_data:
                    updates.append(room_data)
            
            if updates:
                # 执行批量更新
                success_count = DB.batch_update_live_rooms(updates)
                
                for i, op in enumerate(operations):
                    results.append(DatabaseResult(
                        success=i < success_count,
                        affected_rows=1 if i < success_count else 0,
                        operation_id=op.room_id
                    ))
            else:
                for op in operations:
                    results.append(DatabaseResult(
                        success=False,
                        error="没有有效的更新数据"
                    ))
        
        except Exception as e:
            logger.error(f"批量更新直播间失败: {e}")
            for op in operations:
                results.append(DatabaseResult(
                    success=False,
                    error=str(e)
                ))
        
        return results
    
    def _batch_insert_record_files(self, operations: List[DatabaseOperation]) -> List[DatabaseResult]:
        """批量插入录制文件记录"""
        results = []
        
        try:
            from luboman.database.models import RecordFile
            
            # 批量插入
            records = []
            for op in operations:
                records.append(op.data)
            
            if records:
                # 执行批量插入
                success_count = RecordFile.batch_create(records)
                
                for i, op in enumerate(operations):
                    results.append(DatabaseResult(
                        success=i < success_count,
                        affected_rows=1 if i < success_count else 0
                    ))
            else:
                for op in operations:
                    results.append(DatabaseResult(
                        success=False,
                        error="没有有效的插入数据"
                    ))
        
        except Exception as e:
            logger.error(f"批量插入录制文件失败: {e}")
            for op in operations:
                results.append(DatabaseResult(
                    success=False,
                    error=str(e)
                ))
        
        return results
    
    def _execute_individual_operations(self, operations: List[DatabaseOperation]) -> List[DatabaseResult]:
        """执行单个操作"""
        results = []
        
        for op in operations:
            try:
                # 这里应该根据具体的操作类型执行相应的数据库操作
                # 现在使用模拟实现
                time.sleep(0.001)  # 模拟数据库操作延迟
                
                results.append(DatabaseResult(
                    success=True,
                    affected_rows=1,
                    operation_id=op.room_id
                ))
            except Exception as e:
                results.append(DatabaseResult(
                    success=False,
                    error=str(e)
                ))
        
        return results
    
    async def queue_operation(self, operation: DatabaseOperation):
        """将操作加入队列"""
        if not self.running:
            logger.warning("数据库管理器未运行，忽略操作")
            return
        
        try:
            await self.operation_queue.put(operation)
        except asyncio.QueueFull:
            logger.error("数据库操作队列已满，丢弃操作")
    
    async def update_room_data_batch(self, room_data_list: List[Dict[str, Any]]):
        """批量更新房间数据"""
        operations = []
        
        for room_data in room_data_list:
            operation = DatabaseOperation(
                operation_type='update',
                table='live_room',
                data=room_data,
                room_id=str(room_data.get('id', '')),
                priority=1
            )
            operations.append(operation)
        
        # 将操作加入队列
        for operation in operations:
            await self.queue_operation(operation)
    
    async def insert_record_files_batch(self, record_files: List[Dict[str, Any]]):
        """批量插入录制文件记录"""
        operations = []
        
        for record_file in record_files:
            operation = DatabaseOperation(
                operation_type='insert',
                table='record_file',
                data=record_file,
                priority=2
            )
            operations.append(operation)
        
        for operation in operations:
            await self.queue_operation(operation)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            **self.stats,
            'queue_size': self.operation_queue.qsize(),
            'pending_operations': len(self.pending_operations),
            'running': self.running
        }


# 数据库操作缓存器
class DatabaseOperationCache:
    """数据库操作缓存器 - 合并相同的操作以减少数据库负载"""
    
    def __init__(self, merge_window: float = 2.0):
        self.merge_window = merge_window  # 合并窗口时间
        self.cached_operations: Dict[str, DatabaseOperation] = {}
        self.cache_timestamps: Dict[str, float] = {}
    
    def cache_operation(self, operation: DatabaseOperation) -> Optional[DatabaseOperation]:
        """缓存操作，如果有相同操作则合并"""
        cache_key = self._get_cache_key(operation)
        current_time = time.time()
        
        # 检查是否有相同的操作在缓存中
        if cache_key in self.cached_operations:
            cached_time = self.cache_timestamps[cache_key]
            
            # 如果在合并窗口内，合并操作
            if current_time - cached_time < self.merge_window:
                self._merge_operation(self.cached_operations[cache_key], operation)
                self.cache_timestamps[cache_key] = current_time
                return None  # 不需要立即执行
        
        # 缓存新操作
        self.cached_operations[cache_key] = operation
        self.cache_timestamps[cache_key] = current_time
        
        return operation
    
    def _get_cache_key(self, operation: DatabaseOperation) -> str:
        """生成缓存键"""
        if operation.operation_type == 'update' and operation.room_id:
            return f"update:live_room:{operation.room_id}"
        else:
            return f"{operation.operation_type}:{operation.table}:{hash(str(operation.data))}"
    
    def _merge_operation(self, cached_op: DatabaseOperation, new_op: DatabaseOperation):
        """合并操作"""
        if cached_op.operation_type == 'update' and new_op.operation_type == 'update':
            # 合并更新数据
            cached_op.data.update(new_op.data)
    
    def flush_expired(self) -> List[DatabaseOperation]:
        """清理过期的缓存操作"""
        current_time = time.time()
        expired_operations = []
        expired_keys = []
        
        for cache_key, timestamp in self.cache_timestamps.items():
            if current_time - timestamp >= self.merge_window:
                expired_operations.append(self.cached_operations[cache_key])
                expired_keys.append(cache_key)
        
        # 清理过期项
        for key in expired_keys:
            del self.cached_operations[key]
            del self.cache_timestamps[key]
        
        return expired_operations


# 全局异步数据库管理器实例
async_database_manager = AsyncDatabaseManager(batch_size=100, batch_timeout=3.0)

# 数据库操作缓存器实例
database_operation_cache = DatabaseOperationCache(merge_window=2.0)


# 扩展现有的DB类以支持批量操作
def extend_db_with_batch_operations():
    """扩展DB类以支持批量操作"""
    try:
        from luboman.database.db import DB
        
        def batch_update_live_rooms(room_data_list: List[Dict[str, Any]]) -> int:
            """批量更新直播间数据"""
            success_count = 0
            
            try:
                # 这里应该实现真正的批量更新SQL
                for room_data in room_data_list:
                    try:
                        DB.update_live_room_operation_data(room_data)
                        success_count += 1
                    except Exception as e:
                        logger.error(f"更新房间数据失败 {room_data.get('id')}: {e}")
                
            except Exception as e:
                logger.error(f"批量更新失败: {e}")
            
            return success_count
        
        # 添加批量操作方法
        DB.batch_update_live_rooms = batch_update_live_rooms
        
        logger.info("DB类批量操作扩展完成")
        
    except ImportError:
        logger.warning("无法导入DB类，跳过批量操作扩展")


# 启动时自动扩展DB类
extend_db_with_batch_operations()
