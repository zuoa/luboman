import asyncio
import datetime
import logging
import time
import types
from typing import Dict, List, Optional, Any, Callable, Tuple
from luboman.core.async_event import async_event_manager, AsyncEvent, AsyncEventType
from luboman.core.async_network import async_network_manager, NetworkRequest
from luboman.core.async_database import async_database_manager
from luboman.core.async_upload import UploadPriority, schedule_bili_submission
from luboman.core.async_utils import run_blocking
from luboman.config import config

logger = logging.getLogger('luboman')


class AsyncLiveBase:
    """异步化的直播基类 - 使用组合模式避免多重继承冲突"""
    
    def __init__(self, plugin_instance):
        # 使用组合而不是继承来避免metaclass冲突
        self.plugin_instance = plugin_instance
        
        # 代理所有属性到插件实例
        self.room_name = plugin_instance.room_name
        self.room_url = plugin_instance.room_url
        self.room_data = plugin_instance.room_data
        self.log_prefix = plugin_instance.log_prefix
        self.raw_stream_url = plugin_instance.raw_stream_url
        self.is_living = plugin_instance.is_living
        self.living_time = plugin_instance.living_time
        self.is_recording = plugin_instance.is_recording
        self._active = plugin_instance._active
        self.suffix = plugin_instance.suffix
        self.fake_headers = plugin_instance.fake_headers
        
        # 异步相关属性
        self.async_tasks: List[asyncio.Task] = []
        self.status_check_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.batch_update_buffer: List[Dict] = []
        self.last_batch_update = time.time()
        self.room_scope_id = str(self.room_data.get('id', ''))
        self._registered_handlers: List[Tuple[str, Callable]] = []
        
        # 录制状态管理
        self.is_recording_starting = False  # 录制启动中标志，避免重复触发
        
        # 替换同步事件管理器：保留插件录制线程，但把线程产生的事件转入异步事件总线。
        self.async_event_manager = async_event_manager
        self._disable_legacy_event_manager()
        self._bridge_plugin_events()
        self._setup_async_event_handlers()
        
        logger.info(f"{self.log_prefix} 使用异步模式初始化")
    
    def __getattr__(self, name):
        """代理所有未定义的属性访问到插件实例"""
        return getattr(self.plugin_instance, name)

    def _disable_legacy_event_manager(self):
        """关闭插件构造时创建的同步事件管理器，避免双事件系统并行运行。"""
        legacy_manager = getattr(self.plugin_instance, 'event_manager', None)
        if legacy_manager:
            try:
                legacy_manager.stop()
            except Exception as e:
                logger.warning(f"{self.log_prefix} 停止旧事件管理器失败: {e}")
            finally:
                self.plugin_instance.event_manager = None

    def _bridge_plugin_events(self):
        """让插件录制线程里的 self.send_event 进入异步事件总线。"""
        def send_event_bridge(_plugin_self, event):
            self.send_event(event)

        self.plugin_instance.send_event = types.MethodType(
            send_event_bridge,
            self.plugin_instance
        )

    def _scoped_handler(self, event_type: str, priority: int = 0):
        """注册当前直播间作用域内的事件处理器，并保存引用用于停止时注销。"""
        def decorator(func):
            handler = self.async_event_manager.register(
                event_type,
                priority=priority,
                room_id=self.room_scope_id
            )(func)
            self._registered_handlers.append((event_type, handler))
            return handler

        return decorator

    def _unregister_async_event_handlers(self):
        """注销当前直播间注册到全局事件总线的处理器，释放闭包中的 self 引用。"""
        for event_type, handler in self._registered_handlers:
            self.async_event_manager.unregister_handler(event_type, handler)
        self._registered_handlers.clear()

    def _sync_state_from_plugin(self):
        """同步插件实例中由 check_live/录制线程更新的动态状态。"""
        self.raw_stream_url = self.plugin_instance.raw_stream_url
        self.is_living = self.plugin_instance.is_living
        self.living_time = self.plugin_instance.living_time
        self.is_recording = self.plugin_instance.is_recording
        self._active = self.plugin_instance._active

    def _sync_state_to_plugin(self):
        """把异步包装层中的状态写回插件实例，供录制线程读取。"""
        self.plugin_instance.raw_stream_url = self.raw_stream_url
        self.plugin_instance.is_living = self.is_living
        self.plugin_instance.living_time = self.living_time
        self.plugin_instance.is_recording = self.is_recording
        self.plugin_instance._active = self._active
    
    def _setup_async_event_handlers(self):
        """设置异步事件处理器"""
        
        @self._scoped_handler(AsyncEventType.EVENT_CHECK_STATUS, priority=1)
        async def async_check_status(event: AsyncEvent):
            """异步状态检查处理器"""
            # 移除频繁的debug日志 - 状态检查每30秒执行一次太频繁
            
            last_living = self.is_living
            last_living_time = self.living_time
            
            # 异步检查直播状态
            self.is_living = await self.async_check_live(is_check_status=True)
            self.plugin_instance.is_living = self.is_living
            self.raw_stream_url = self.plugin_instance.raw_stream_url
            self.room_data['live_state'] = 1 if self.is_living else 0
            
            if self.is_living:
                self.living_time = int(time.time() * 1000)
                self.plugin_instance.living_time = self.living_time
                self.room_data['last_living_time'] = datetime.datetime.now()
                
                # 异步下载资源
                await self.async_send_event(AsyncEvent(
                    AsyncEventType.EVENT_DOWNLOAD_ASSET,
                    priority=3
                ))
            
            # 状态改变记录
            if last_living != self.is_living:
                logger.info(f'{self.log_prefix} living: {self.is_living}, last_living: {last_living}')
                
                # 批量数据库更新
                await self._queue_database_update()
                
                # 开播通知
                if self.is_living and self.living_time - last_living_time > 60000:
                    await self.async_send_event(AsyncEvent(
                        AsyncEventType.EVENT_NOTIFY,
                        args=(f'开播通知:{self.room_name}',
                              f'### {self.room_name}[{self.room_data.get("room_id")}]开播了\n\n'
                              f'{self.room_data.get("room_title")}\n\n{self.room_url}'),
                        priority=2
                    ))
            
            # 启动录制 - 加入中间状态判断，避免重复触发
            if self.is_living and not self.is_recording and not self.is_recording_starting:
                self.is_recording_starting = True  # 立即设置标志，避免重复触发
                logger.info(f'{self.log_prefix} 准备启动录制，设置录制启动中标志')
                await self.async_send_event(AsyncEvent(
                    AsyncEventType.EVENT_PRE_RECORD,
                    priority=1
                ))
        
        @self._scoped_handler(AsyncEventType.EVENT_REFRESH_ROOM_INFO, priority=1)
        async def async_refresh_room_info(event: AsyncEvent):
            """房间配置刷新处理器：把页面更新后的配置同步到运行中的插件实例。"""
            room_info = event.args[0] if event.args else None
            if isinstance(room_info, dict) and room_info:
                self.room_data.update(room_info)
                self.plugin_instance.room_data = self.room_data
                logger.info(f'{self.log_prefix} 房间配置已刷新')

        @self._scoped_handler(AsyncEventType.EVENT_DOWNLOAD_ASSET, priority=3)
        async def async_download_assets(event: AsyncEvent):
            """异步下载资源处理器"""
            assets_to_download = []
            
            # 封面下载
            cover_url = (self.room_data.get('room_cover_frame_url') or 
                        self.room_data.get('room_cover_url'))
            if cover_url:
                assets_to_download.append({
                    'url': cover_url,
                    'type': 'cover',
                    'file_path': f'cover/{self.room_data.get("room_platform")}-{self.room_data.get("room_id")}.jpg',
                    'room_id': str(self.room_data.get('id')),
                    'headers': self.fake_headers
                })
            
            # 头像下载
            avatar_url = self.room_data.get('room_owner_avatar')
            if avatar_url:
                assets_to_download.append({
                    'url': avatar_url,
                    'type': 'avatar',
                    'file_path': f'avatar/{self.room_data.get("room_platform")}-{self.room_data.get("room_id")}.jpg',
                    'room_id': str(self.room_data.get('id')),
                    'headers': self.fake_headers
                })
            
            if assets_to_download:
                await self._batch_download_assets(assets_to_download)
        
        @self._scoped_handler(AsyncEventType.EVENT_UPLOAD_BILI, priority=4)
        async def async_process_upload_bili(event: AsyncEvent):
            """异步B站上传处理器"""
            file_list = event.args[0] if event.args else []
            
            logger.info(f'{self.log_prefix} 异步Bili上传开始: {len(file_list)} 个文件')
            
            # 获取上传配置
            bili_upload_template_id = self.room_data.get('bili_upload_template_id')
            if not bili_upload_template_id:
                logger.error(f"{self.log_prefix} bili_upload_template_id is None")
                return
            
            # 异步获取模板信息
            template_info = await self._async_get_bili_template(bili_upload_template_id)
            if not template_info:
                return
            
            # 过滤文件
            prepare_upload_file_list = await self._filter_upload_files(file_list)
            
            if prepare_upload_file_list:
                result = await schedule_bili_submission(
                    file_list=prepare_upload_file_list,
                    room_data={**self.room_data, 'bili_upload_template': template_info},
                    source='AUTO',
                    priority=UploadPriority.HIGH,
                    metadata={'created_from': 'auto_record'},
                )
                logger.info(f'{self.log_prefix} B站投稿任务已创建: {result}')
        
        @self._scoped_handler(AsyncEventType.EVENT_PRE_RECORD, priority=1)
        async def async_process_pre_record(event: AsyncEvent):
            """异步录制预处理器"""
            logger.info(f'{self.log_prefix} 收到录制预处理事件，准备开始录制')
            # 精简debug日志：录制状态信息已在info日志中体现
            await self.async_send_event(AsyncEvent(
                AsyncEventType.EVENT_RECORD,
                priority=1
            ))
        
        @self._scoped_handler(AsyncEventType.EVENT_RECORD, priority=1)
        async def async_process_record(event: AsyncEvent):
            """异步录制处理器"""
            logger.info(f'{self.log_prefix} 收到录制事件，开始录制')
            
            try:
                # 在线程池中执行录制，避免阻塞异步事件循环
                await run_blocking(self.plugin_instance._start_record)
                
                # 录制启动完成后清除中间状态标志
                self._sync_state_from_plugin()
                self.is_recording_starting = False
                logger.info(f'{self.log_prefix} 录制启动完成，清除录制启动中标志，当前状态: is_recording={self.is_recording}')
                
            except Exception as e:
                # 录制启动失败时也要清除标志，避免状态锁死
                self.is_recording_starting = False
                logger.error(f'{self.log_prefix} 录制启动失败: {e}，已清除录制启动中标志')
                raise
        
        @self._scoped_handler(AsyncEventType.EVENT_RECORD_COMPLETED, priority=3)
        async def async_process_record_completed(event: AsyncEvent):
            """异步录制完成处理器"""
            file_list = event.args[0] if event.args else []
            logger.info(f'{self.log_prefix} 录制完成，处理 {len(file_list)} 个文件')
            
            # 录制完成时也要清除录制启动中标志，防止状态异常
            self.is_recording_starting = False
            self.is_recording = False
            self.is_living = False
            self._sync_state_to_plugin()
            
            # 如果开启了自动上传
            if self.room_data.get('auto_upload', False):
                await self.async_send_event(AsyncEvent(
                    AsyncEventType.EVENT_UPLOAD,
                    args=(file_list,),
                    priority=4
                ))
            
            # 如果开启了B站上传
            if self.room_data.get('bili_upload_template_id'):
                await self.async_send_event(AsyncEvent(
                    AsyncEventType.EVENT_UPLOAD_BILI,
                    args=(file_list,),
                    priority=4
                ))
        
        @self._scoped_handler(AsyncEventType.EVENT_NOTIFY, priority=2)
        async def async_process_notify(event: AsyncEvent):
            """异步通知处理器"""
            if len(event.args) >= 2:
                title, content = event.args[0], event.args[1]
                logger.info(f'{self.log_prefix} 发送通知: {title}')
                
                # 在线程池中执行通知发送，避免阻塞
                await run_blocking(self._send_notification_sync, title, content)
        
        @self._scoped_handler(AsyncEventType.EVENT_UPLOAD, priority=4)
        async def async_process_upload(event: AsyncEvent):
            """异步上传处理器"""
            file_list = event.args[0] if event.args else []
            
            prepare_upload_file_list = await self._filter_upload_files(file_list)
            
            if prepare_upload_file_list:
                await self._async_upload_to_storage(prepare_upload_file_list)
                
                await self.async_send_event(AsyncEvent(
                    AsyncEventType.EVENT_UPLOAD_COMPLETED,
                    args=(prepare_upload_file_list,),
                    priority=5
                ))
    
    async def async_start(self):
        """异步启动直播间"""
        logger.info(f'{self.log_prefix} 异步启动直播间')
        self._loop = asyncio.get_running_loop()
        self._active = True
        self.plugin_instance._active = True
        
        # 启动异步状态检查任务
        self.status_check_task = asyncio.create_task(
            self._async_status_check_loop(),
            name=f"status-check-{self.room_data.get('id')}"
        )
        self.async_tasks.append(self.status_check_task)
        
        # 不再调用插件实例 start()，避免启动旧的同步状态检查循环。
    
    async def async_stop(self):
        """异步停止直播间"""
        logger.warning(f'{self.log_prefix} 异步停止直播间')
        
        # 设置停止标志
        self._active = False
        
        # 清除录制相关状态标志
        self.is_recording_starting = False
        
        # 取消所有异步任务
        for task in self.async_tasks:
            if not task.done():
                task.cancel()
        
        # 等待任务完成
        if self.async_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.async_tasks, return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning(f'{self.log_prefix} 异步任务停止超时')
        
        # 处理剩余的批量更新
        if self.batch_update_buffer:
            await self._flush_batch_updates()
        
        # 调用插件实例的停止方法
        if hasattr(self.plugin_instance, 'stop'):
            self.plugin_instance.stop()

        self._unregister_async_event_handlers()
        
        logger.info(f'{self.log_prefix} 异步直播间已停止')
    
    async def _async_status_check_loop(self):
        """异步状态检查循环"""
        seq = 1
        
        while self._active:
            try:
                # 发送状态检查事件
                await self.async_send_event(AsyncEvent(
                    AsyncEventType.EVENT_CHECK_STATUS,
                    args=(seq,),
                    priority=1
                ))
                
                # 每10次检查更新一次数据库（但通过批量处理）
                if seq % 10 == 0:
                    await self._queue_database_update()
                
                # 每100次检查记录一次debug信息（50分钟一次），避免完全沉默
                if seq % 100 == 0:
                    logger.info(f'{self.log_prefix} 状态检查运行中，已执行 {seq} 次')
                
                seq += 1
                await asyncio.sleep(config.get_live_check_interval())  # 检测间隔可配置
                
            except asyncio.CancelledError:
                # 取消操作是正常流程，不需要debug日志
                break
            except Exception as e:
                logger.error(f'{self.log_prefix} 状态检查循环错误: {e}')
                await asyncio.sleep(5)  # 错误后等待5秒
    
    async def async_check_live(self, is_check_status=False) -> bool:
        """异步检查直播状态"""
        try:
            # 在线程池中执行具体插件的check_live方法
            # 这样可以保持与原有插件逻辑的完全兼容
            result = await run_blocking(self.plugin_instance.check_live, is_check_status)
            self._sync_state_from_plugin()
            return result
            
        except Exception as e:
            logger.error(f'{self.log_prefix} 异步状态检查失败: {e}')
            return False
    
    async def _queue_database_update(self):
        """将数据库更新加入批量处理队列"""
        update_columns = {
            'id', 'room_platform', 'room_id', 'room_title', 'room_owner_id',
            'room_owner', 'room_owner_avatar', 'room_owner_title',
            'room_cover_url', 'room_cover_frame_url', 'live_state',
            'status', 'last_living_time'
        }
        room_data_copy = {
            key: value for key, value in self.room_data.items()
            if key in update_columns
        }
        self.batch_update_buffer.append(room_data_copy)
        
        current_time = time.time()
        
        # 如果缓冲区满了或者距离上次更新超过10秒，执行批量更新
        if (len(self.batch_update_buffer) >= 5 or 
            current_time - self.last_batch_update > 10.0):
            await self._flush_batch_updates()
    
    async def _flush_batch_updates(self):
        """执行批量数据库更新"""
        if not self.batch_update_buffer:
            return
        
        try:
            await async_database_manager.update_room_data_batch(
                self.batch_update_buffer.copy()
            )
            
            self.batch_update_buffer.clear()
            self.last_batch_update = time.time()
            
        except Exception as e:
            logger.error(f'{self.log_prefix} 批量数据库更新失败: {e}')
    
    async def _batch_download_assets(self, assets: List[Dict]):
        """批量下载资源"""
        try:
            responses = await async_network_manager.batch_download_assets(assets)
            
            success_count = 0
            for i, response in enumerate(responses):
                if response.success:
                    success_count += 1
                    # 保存文件
                    await self._save_asset_file(assets[i], response.data)
                else:
                    logger.warning(f'{self.log_prefix} 资源下载失败: {response.error}')
            
            logger.info(f'{self.log_prefix} 批量资源下载完成: {success_count}/{len(assets)}')
            
        except Exception as e:
            logger.error(f'{self.log_prefix} 批量资源下载异常: {e}')
    
    async def _save_asset_file(self, asset_info: Dict, file_data: Any):
        """保存资源文件"""
        try:
            import os
            from luboman.core.utils import get_public_dir
            
            file_path = f"{get_public_dir()}/{asset_info['file_path']}"
            file_dir = os.path.dirname(file_path)
            
            if not os.path.exists(file_dir):
                os.makedirs(file_dir)
            
            # 在线程中执行文件写入
            await run_blocking(self._write_file_sync, file_path, file_data)
            
        except Exception as e:
            logger.error(f'{self.log_prefix} 保存资源文件失败: {e}')
    
    def _write_file_sync(self, file_path: str, file_data: Any):
        """同步写入文件"""
        if isinstance(file_data, str):
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(file_data)
        else:
            with open(file_path, 'wb') as f:
                f.write(file_data)
    
    def _send_notification_sync(self, title: str, content: str):
        """同步发送通知"""
        try:
            # 这里可以集成各种通知方式（钉钉、微信、邮件等）
            # 目前只记录日志
            logger.info(f'{self.log_prefix} 通知标题: {title}')
            logger.info(f'{self.log_prefix} 通知内容: {content}')
            
            # 如果有配置的通知方式，可以在这里调用
            # 例如调用父类的通知方法或者第三方通知服务
            
        except Exception as e:
            logger.error(f'{self.log_prefix} 发送通知失败: {e}')
    
    async def _async_get_bili_template(self, template_id) -> Optional[Dict]:
        """异步获取B站上传模板"""
        try:
            # 在线程中执行数据库查询
            from luboman.database.models import BiliUploadTemplate, BiliAccount
            from playhouse.shortcuts import model_to_dict
            
            def get_template_sync():
                template_info = BiliUploadTemplate.get_by_id_(template_id)
                if not template_info:
                    return None
                
                if template_info.bili_account_id is None:
                    return None
                
                bili_account = BiliAccount.get_by_id_(template_info.bili_account_id)
                if not bili_account:
                    return None
                
                template_dict = model_to_dict(template_info)
                template_dict['bili_account'] = model_to_dict(bili_account)
                return template_dict
            
            return await run_blocking(get_template_sync)
            
        except Exception as e:
            logger.error(f'{self.log_prefix} 获取B站模板失败: {e}')
            return None
    
    async def _filter_upload_files(self, file_list: List[Dict]) -> List[Dict]:
        """异步过滤上传文件"""
        try:
            from luboman.config import config
            import os
            
            def filter_files_sync():
                prepare_upload_file_list = []
                filtering_threshold_file_size = config.get("filtering_threshold_file_size", 5)
                filtering_threshold_file_size = int(filtering_threshold_file_size) * 1024 * 1024
                
                for file in file_list:
                    video_path = file.get('video', '')
                    if os.path.exists(video_path) and os.path.getsize(video_path) >= filtering_threshold_file_size:
                        prepare_upload_file_list.append(file)
                
                return prepare_upload_file_list
            
            return await run_blocking(filter_files_sync)
            
        except Exception as e:
            logger.error(f'{self.log_prefix} 过滤上传文件失败: {e}')
            return []
    
    async def _async_upload_to_storage(self, file_list: List[Dict]):
        """异步上传到存储"""
        try:
            upload_platform = self.room_data.get('upload_storage_platform')
            if upload_platform:
                # 在线程中执行上传
                from luboman.core.upload import upload
                result = await run_blocking(upload, upload_platform, file_list)
                logger.info(f'{self.log_prefix} 异步存储上传完成: {result}')
                
        except Exception as e:
            logger.error(f'{self.log_prefix} 异步存储上传失败: {e}')
    
    async def async_send_event(self, event: AsyncEvent):
        """发送异步事件"""
        if event.room_id is None:
            event.room_id = self.room_scope_id
            event.data.setdefault('room_id', self.room_scope_id)
        if event.type_ in (AsyncEventType.EVENT_UPLOAD, AsyncEventType.EVENT_UPLOAD_BILI):
            event.data.setdefault('room_data', self.room_data.copy())
        await self.async_event_manager.send_event(event)
    
    # 保持与原有接口的兼容性
    def send_event(self, event):
        """兼容原有的同步事件发送接口"""
        # 转换为异步事件
        if hasattr(event, 'type_'):
            async_event = AsyncEvent(
                event.type_,
                event.args if hasattr(event, 'args') else (),
                getattr(event, 'data', {}),
                room_id=getattr(event, 'room_id', None)
            )
        else:
            async_event = AsyncEvent(str(event))
        
        # 尝试在当前事件循环中发送
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.async_send_event(async_event))
        except RuntimeError:
            if self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self.async_send_event(async_event))
                )
            else:
                logger.warning(f'{self.log_prefix} 在非异步上下文中发送事件但事件循环不可用: {event}')


# 异步直播间批量管理器
class AsyncLiveRoomManager:
    """异步直播间批量管理器"""
    
    def __init__(self, enable_batch_status_check: bool = False):
        self.live_rooms: Dict[str, AsyncLiveBase] = {}
        self.batch_check_task: Optional[asyncio.Task] = None
        self.running = False
        self.enable_batch_status_check = enable_batch_status_check
    
    async def start(self):
        """启动批量管理器"""
        if self.running:
            return
        
        self.running = True
        logger.info("启动异步直播间批量管理器")
        
        if self.enable_batch_status_check:
            self.batch_check_task = asyncio.create_task(
                self._batch_status_check_loop(),
                name="batch-status-check"
            )
    
    async def stop(self):
        """停止批量管理器"""
        if not self.running:
            return
        
        self.running = False
        logger.info("停止异步直播间批量管理器")
        
        # 停止批量检查任务
        if self.batch_check_task and not self.batch_check_task.done():
            self.batch_check_task.cancel()
            try:
                await self.batch_check_task
            except asyncio.CancelledError:
                pass
        
        # 停止所有直播间
        stop_tasks = []
        for room in self.live_rooms.values():
            stop_tasks.append(room.async_stop())
        
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
        
        self.live_rooms.clear()
    
    async def add_room(self, room: AsyncLiveBase):
        """添加直播间"""
        room_id = str(room.room_data.get('id', ''))
        self.live_rooms[room_id] = room
        await room.async_start()
    
    async def remove_room(self, room_id: str):
        """移除直播间"""
        if room_id in self.live_rooms:
            room = self.live_rooms.pop(room_id)
            await room.async_stop()

    def get_stats(self) -> Dict[str, Any]:
        """获取直播间管理器运行状态。"""
        return {
            'running': self.running,
            'rooms_count': len(self.live_rooms),
            'room_ids': list(self.live_rooms.keys()),
            'batch_status_check_enabled': self.enable_batch_status_check,
            'batch_check_task_running': (
                self.batch_check_task is not None and
                not self.batch_check_task.done()
            )
        }
    
    async def _batch_status_check_loop(self):
        """批量状态检查循环"""
        while self.running:
            try:
                if self.live_rooms:
                    # 批量状态检查逻辑
                    await self._perform_batch_status_check()
                
                await asyncio.sleep(30)  # 30秒执行一次批量检查
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"批量状态检查失败: {e}")
                await asyncio.sleep(10)
    
    async def _perform_batch_status_check(self):
        """执行批量状态检查"""
        room_list = list(self.live_rooms.values())
        
        # 构造批量网络请求
        requests = []
        for room in room_list:
            request = NetworkRequest(
                url=room.room_url,
                method='GET',
                headers=room.fake_headers,
                timeout=15.0,
                room_id=str(room.room_data.get('id', '')),
                request_type='batch_status_check',
                priority=1
            )
            requests.append(request)
        
        if requests:
            # 执行批量网络请求
            responses = await async_network_manager.batch_requests(requests)
            
            # 处理响应
            for i, response in enumerate(responses):
                if i < len(room_list):
                    room = room_list[i]
                    
                    if response.success:
                        # 更新房间状态
                        old_status = room.is_living
                        if hasattr(room.plugin_instance, '_parse_live_status'):
                            room.is_living = room.plugin_instance._parse_live_status(response.data)
                        elif hasattr(room.plugin_instance, 'parse_live_status'):
                            room.is_living = room.plugin_instance.parse_live_status(response.data)
                        
                        if old_status != room.is_living:
                            logger.info(f'批量检查状态变化 {room.room_name}: {old_status} -> {room.is_living}')
                    else:
                        logger.warning(f'批量状态检查失败 {room.room_name}: {response.error}')


# 全局异步直播间管理器
async_live_room_manager = AsyncLiveRoomManager()
