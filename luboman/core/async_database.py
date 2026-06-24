import asyncio
import logging
import time
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

from luboman.core.async_utils import run_blocking

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
        self._drain_operation_queue()
        
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
                    self.operation_queue.task_done()
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

    def _drain_operation_queue(self):
        """停止前把队列中尚未进入批次缓冲的操作取出。"""
        while True:
            try:
                operation = self.operation_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.pending_operations.append(operation)
            self.operation_queue.task_done()
    
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
                results = await run_blocking(self._execute_batch_sync, grouped_operations)
                
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
            
            merged_by_id: Dict[str, Dict[str, Any]] = {}
            for op in operations:
                room_data = op.data
                row_id = room_data.get('id') or op.room_id
                if row_id:
                    existing = merged_by_id.setdefault(str(row_id), {'id': row_id})
                    existing.update(room_data)
            
            if merged_by_id:
                # 执行批量更新
                success_count = DB.batch_update_live_rooms(list(merged_by_id.values()))
                updated_ids = set(list(merged_by_id.keys())[:success_count])

                for op in operations:
                    row_id = str(op.data.get('id') or op.room_id)
                    success = row_id in updated_ids
                    results.append(DatabaseResult(
                        success=success,
                        affected_rows=1 if success else 0,
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
            from luboman.database.models import RecordFile, db
            
            # 批量插入
            records = []
            for op in operations:
                records.append(op.data)
            
            if records:
                # 执行批量插入
                record_models = [RecordFile(**record) for record in records]
                with db.connection_context():
                    with db.atomic():
                        RecordFile.bulk_create(record_models, batch_size=100)
                success_count = len(record_models)
                
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
            start_time = time.time()
            try:
                result = self._execute_single_operation(op)
                result.execution_time = time.time() - start_time
                results.append(result)
            except Exception as e:
                results.append(DatabaseResult(
                    success=False,
                    error=str(e),
                    operation_id=op.room_id,
                    execution_time=time.time() - start_time
                ))
        
        return results

    def _get_model(self, table: str):
        from luboman.database.models import LiveRoom, RecordFile, GlobalConfig, BiliAccount, BiliUploadTemplate

        model_map = {
            'live_room': LiveRoom,
            'record_file': RecordFile,
            'global_config': GlobalConfig,
            'bili_account': BiliAccount,
            'bili_upload_template': BiliUploadTemplate,
        }
        return model_map.get(table)

    def _filter_model_data(self, model, data: Dict[str, Any]) -> Dict[str, Any]:
        fields = model._meta.fields
        return {key: value for key, value in data.items() if key in fields}

    def _build_where_expression(self, model, where_clause: Optional[Dict[str, Any]]):
        if not where_clause:
            return None

        expression = None
        for key, value in where_clause.items():
            if key not in model._meta.fields:
                continue
            condition = getattr(model, key) == value
            expression = condition if expression is None else expression & condition
        return expression

    def _execute_single_operation(self, operation: DatabaseOperation) -> DatabaseResult:
        from luboman.database.models import db

        model = self._get_model(operation.table)
        if model is None:
            return DatabaseResult(
                success=False,
                error=f"不支持的表: {operation.table}",
                operation_id=operation.room_id
            )

        data = self._filter_model_data(model, operation.data or {})

        with db.connection_context():
            if operation.operation_type == 'insert':
                created = model.create(**data)
                return DatabaseResult(
                    success=True,
                    affected_rows=1,
                    operation_id=str(getattr(created, 'id', operation.room_id))
                )

            if operation.operation_type == 'update':
                where_clause = operation.where_clause or {}
                if not where_clause and 'id' in data:
                    where_clause = {'id': data['id']}
                    data = {key: value for key, value in data.items() if key != 'id'}

                expression = self._build_where_expression(model, where_clause)
                if expression is None or not data:
                    return DatabaseResult(
                        success=False,
                        error="更新操作缺少 where_clause 或更新字段",
                        operation_id=operation.room_id
                    )

                affected = model.update(**data).where(expression).execute()
                return DatabaseResult(
                    success=True,
                    affected_rows=affected,
                    operation_id=operation.room_id
                )

            if operation.operation_type == 'delete':
                expression = self._build_where_expression(model, operation.where_clause)
                if expression is None:
                    return DatabaseResult(
                        success=False,
                        error="删除操作缺少 where_clause",
                        operation_id=operation.room_id
                    )

                affected = model.delete().where(expression).execute()
                return DatabaseResult(
                    success=True,
                    affected_rows=affected,
                    operation_id=operation.room_id
                )

            if operation.operation_type == 'select':
                from playhouse.shortcuts import model_to_dict

                expression = self._build_where_expression(model, operation.where_clause)
                query = model.select()
                if expression is not None:
                    query = query.where(expression)
                rows = [model_to_dict(row) for row in query]
                return DatabaseResult(
                    success=True,
                    affected_rows=len(rows),
                    operation_id=operation.room_id,
                    data=rows
                )

        return DatabaseResult(
            success=False,
            error=f"不支持的操作类型: {operation.operation_type}",
            operation_id=operation.room_id
        )
    
    async def queue_operation(self, operation: DatabaseOperation):
        """将操作加入队列"""
        if not self.running:
            logger.warning("数据库管理器未运行，忽略操作")
            return
        
        try:
            self.operation_queue.put_nowait(operation)
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


# 全局异步数据库管理器实例
async_database_manager = AsyncDatabaseManager(batch_size=100, batch_timeout=3.0)
