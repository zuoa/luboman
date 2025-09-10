import asyncio
import logging
import time
import copy
from typing import Dict, List, Optional, Any
from luboman.core.async_event import async_event_manager, AsyncEvent, AsyncEventType
from luboman.core.async_network import async_network_manager, NetworkRequest
from luboman.core.async_database import async_database_manager, DatabaseOperation
from luboman.core.live import LiveBase

logger = logging.getLogger('luboman')


class AsyncLiveBase(LiveBase):
    """异步化的直播基类 - 继承原有功能并添加异步支持"""
    
    def __init__(self, room_name, room_url, suffix):
        # 初始化父类
        super().__init__(room_name, room_url, suffix)
        
        # 异步相关属性
        self.async_tasks: List[asyncio.Task] = []
        self.status_check_task: Optional[asyncio.Task] = None
        self.batch_update_buffer: List[Dict] = []
        self.last_batch_update = time.time()
        
        # 替换同步事件管理器
        self.async_event_manager = async_event_manager
        self._setup_async_event_handlers()
        
        logger.info(f"{self.log_prefix} 使用异步模式初始化")
    
    def _setup_async_event_handlers(self):
        """设置异步事件处理器"""
        
        @self.async_event_manager.register(AsyncEventType.EVENT_CHECK_STATUS, priority=1)
        async def async_check_status(event: AsyncEvent):
            """异步状态检查处理器"""
            logger.debug(f'{self.log_prefix} 异步检查直播状态')
            
            last_living = self.is_living
            last_living_time = self.living_time
            
            # 异步检查直播状态
            self.is_living = await self.async_check_live(is_check_status=True)
            self.room_data['live_state'] = 1 if self.is_living else 0
            
            if self.is_living:
                self.living_time = int(time.time() * 1000)
                self.room_data['last_living_time'] = time.time()
                
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
            
            # 启动录制
            if self.is_living and not self.is_recording:
                await self.async_send_event(AsyncEvent(
                    AsyncEventType.EVENT_PRE_RECORD,
                    priority=1
                ))
        
        @self.async_event_manager.register(AsyncEventType.EVENT_DOWNLOAD_ASSET, priority=3)
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
        
        @self.async_event_manager.register(AsyncEventType.EVENT_UPLOAD_BILI, priority=4)
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
                # 异步上传
                upload_info = {
                    'room_data': {**self.room_data, 'bili_upload_template': template_info}
                }
                
                # 这里应该调用异步上传功能
                # 目前保持与原有上传系统的兼容
                from luboman.core.upload import upload
                result = await asyncio.get_event_loop().run_in_executor(
                    None, upload, 'biliweb', prepare_upload_file_list, **upload_info
                )
                
                logger.info(f'{self.log_prefix} 异步Bili上传完成: {result}')
        
        @self.async_event_manager.register(AsyncEventType.EVENT_UPLOAD, priority=4)
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
        
        # 启动异步状态检查任务
        self.status_check_task = asyncio.create_task(
            self._async_status_check_loop(),
            name=f"status-check-{self.room_data.get('id')}"
        )
        self.async_tasks.append(self.status_check_task)
        
        # 调用父类启动方法（如果需要）
        super().start()
    
    async def async_stop(self):
        """异步停止直播间"""
        logger.warning(f'{self.log_prefix} 异步停止直播间')
        
        # 设置停止标志
        self._active = False
        
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
        
        # 调用父类停止方法
        super().stop()
        
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
                
                seq += 1
                await asyncio.sleep(30)  # 30秒检查一次
                
            except asyncio.CancelledError:
                logger.debug(f'{self.log_prefix} 状态检查循环被取消')
                break
            except Exception as e:
                logger.error(f'{self.log_prefix} 状态检查循环错误: {e}')
                await asyncio.sleep(5)  # 错误后等待5秒
    
    async def async_check_live(self, is_check_status=False) -> bool:
        """异步检查直播状态"""
        try:
            # 创建网络请求
            request = NetworkRequest(
                url=self.room_url,  # 实际应用中应该是状态检查API
                method='GET',
                headers=self.fake_headers,
                timeout=15.0,
                room_id=str(self.room_data.get('id', '')),
                request_type='status_check',
                priority=1
            )
            
            # 执行异步请求
            response = await async_network_manager.single_request(request)
            
            if response.success:
                # 这里应该解析实际的API响应来判断直播状态
                # 现在使用模拟逻辑
                return self._parse_live_status(response.data)
            else:
                logger.warning(f'{self.log_prefix} 状态检查请求失败: {response.error}')
                return False
                
        except Exception as e:
            logger.error(f'{self.log_prefix} 异步状态检查失败: {e}')
            return False
    
    def _parse_live_status(self, response_data) -> bool:
        """解析直播状态 - 子类应该重写此方法"""
        # 这里应该根据不同平台的API响应格式来解析
        # 现在返回父类的同步检查结果作为兜底
        try:
            return self.check_live(is_check_status=True)
        except Exception:
            return False
    
    async def _queue_database_update(self):
        """将数据库更新加入批量处理队列"""
        room_data_copy = copy.deepcopy(self.room_data)
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
            await asyncio.get_event_loop().run_in_executor(
                None, self._write_file_sync, file_path, file_data
            )
            
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
            
            return await asyncio.get_event_loop().run_in_executor(
                None, get_template_sync
            )
            
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
            
            return await asyncio.get_event_loop().run_in_executor(
                None, filter_files_sync
            )
            
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
                result = await asyncio.get_event_loop().run_in_executor(
                    None, upload, upload_platform, file_list
                )
                logger.info(f'{self.log_prefix} 异步存储上传完成: {result}')
                
        except Exception as e:
            logger.error(f'{self.log_prefix} 异步存储上传失败: {e}')
    
    async def async_send_event(self, event: AsyncEvent):
        """发送异步事件"""
        await self.async_event_manager.send_event(event)
    
    # 保持与原有接口的兼容性
    def send_event(self, event):
        """兼容原有的同步事件发送接口"""
        # 转换为异步事件
        if hasattr(event, 'type_'):
            async_event = AsyncEvent(
                event.type_,
                event.args if hasattr(event, 'args') else (),
                getattr(event, 'data', {})
            )
        else:
            async_event = AsyncEvent(str(event))
        
        # 尝试在当前事件循环中发送
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(self.async_send_event(async_event))
        except RuntimeError:
            # 如果不在事件循环中，记录警告
            logger.warning(f'{self.log_prefix} 在非异步上下文中发送事件: {event}')


# 异步直播间批量管理器
class AsyncLiveRoomManager:
    """异步直播间批量管理器"""
    
    def __init__(self):
        self.live_rooms: Dict[str, AsyncLiveBase] = {}
        self.batch_check_task: Optional[asyncio.Task] = None
        self.running = False
    
    async def start(self):
        """启动批量管理器"""
        if self.running:
            return
        
        self.running = True
        logger.info("启动异步直播间批量管理器")
        
        # 启动批量状态检查任务
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
                        room.is_living = room._parse_live_status(response.data)
                        
                        if old_status != room.is_living:
                            logger.info(f'批量检查状态变化 {room.room_name}: {old_status} -> {room.is_living}')
                    else:
                        logger.warning(f'批量状态检查失败 {room.room_name}: {response.error}')


# 全局异步直播间管理器
async_live_room_manager = AsyncLiveRoomManager()
