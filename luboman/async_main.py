"""
异步化主程序 - 基于异步架构重构的主要入口点
解决线程池积压问题，大幅提升系统性能
"""

import asyncio
import atexit
import datetime
import logging.config
import os
import signal
import sys
import time
from typing import Dict, List, Optional

# 导入原有模块
from luboman.config import config
from luboman.core import bili_account_health
from luboman.core.decorators import PluginTool
from luboman import __version__, LOG_CONF
from luboman.core.notify import notify_message
from luboman.core.utils import get_video_dir, remove_dir
from luboman.database.db import DB
from luboman import plugins

# 导入新的异步组件
from luboman.core.async_event import async_event_manager, AsyncEventType
from luboman.core.async_network import async_network_manager
from luboman.core.async_database import async_database_manager
from luboman.core.async_live import AsyncLiveBase, async_live_room_manager
from luboman.core.async_upload import async_upload_scheduler, upload_event_handler
from luboman.core.dance_clip import clip_scheduler
from luboman.core.async_utils import run_blocking
from luboman.core.runtime import start_room_runtime, stop_room_runtime

logger = logging.getLogger("luboman")


class AsyncLubomanApplication:
    """异步化的Luboman应用程序"""
    
    def __init__(self):
        self.running = False
        self.cleanup_done = False
        
        # 异步组件
        self.components = [
            async_event_manager,
            async_network_manager,
            async_database_manager,
            async_upload_scheduler,
            clip_scheduler,
            async_live_room_manager
        ]
        
        # 定时任务
        self.timer_tasks: List[asyncio.Task] = []

        # 已告警过的失效账号 id 集合，避免对同一个死账号反复推送；账号恢复后自动移出
        self._alerted_account_ids: set = set()
        
        # 监控任务
        self.monitor_task: Optional[asyncio.Task] = None
        
        # Web服务任务
        self.web_task: Optional[asyncio.Task] = None
        
        # 直播间实例
        self.live_rooms: Dict[str, AsyncLiveBase] = {}
    
    async def start(self):
        """启动应用程序"""
        if self.running:
            return
        
        logger.info("="*60)
        logger.info(f"启动异步化Luboman v{__version__}")
        logger.info("="*60)
        self.running = True
        self.cleanup_done = False
        
        try:
            # 启动所有异步组件
            await self._start_components()
            
            # 设置事件处理器
            self._setup_event_handlers()
            
            # 启动直播间监控
            await self._start_live_rooms()
            
            # 启动定时任务
            await self._start_timers()
            
            # 启动系统监控
            await self._start_monitoring()
            
            # 启动Web服务
            await self._start_web_service()
            
            logger.info("异步化Luboman启动完成")
            
        except Exception as e:
            logger.error(f"应用程序启动失败: {e}")
            await self.stop()
            raise
    
    async def stop(self):
        """停止应用程序"""
        if self.cleanup_done:
            return
        
        logger.info("开始关闭异步化Luboman...")
        self.running = False
        self.cleanup_done = True
        
        try:
            # 停止Web服务
            if self.web_task and not self.web_task.done():
                self.web_task.cancel()
                try:
                    await asyncio.wait_for(self.web_task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            
            # 停止监控任务
            if self.monitor_task and not self.monitor_task.done():
                self.monitor_task.cancel()
                try:
                    await self.monitor_task
                except asyncio.CancelledError:
                    pass
            
            # 停止定时任务
            for timer_task in self.timer_tasks:
                if not timer_task.done():
                    timer_task.cancel()
            
            if self.timer_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*self.timer_tasks, return_exceptions=True),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    logger.warning("定时任务停止超时")
            
            # 停止直播间
            await self._stop_live_rooms()
            
            # 停止异步组件（逆序）
            for component in reversed(self.components):
                try:
                    await component.stop()
                except Exception as e:
                    logger.error(f"停止组件失败: {e}")
            
            # 清理传统插件
            self._cleanup_legacy_plugins()
            
            logger.info("异步化Luboman已完全关闭")
            
        except Exception as e:
            logger.error(f"应用程序关闭时出错: {e}")
    
    async def _start_components(self):
        """启动所有异步组件"""
        for component in self.components:
            component_name = component.__class__.__name__
            try:
                logger.info(f"启动组件: {component_name}")
                await component.start()
                logger.info(f"组件启动成功: {component_name}")
            except Exception as e:
                logger.error(f"启动组件失败 {component_name}: {e}")
                raise
    
    def _setup_event_handlers(self):
        """设置事件处理器"""
        logger.info("设置事件处理器")
        
        # 注册上传事件处理器
        async_event_manager.register_handler(
            AsyncEventType.EVENT_UPLOAD,
            upload_event_handler.handle_upload_event,
            priority=1
        )
        
        async_event_manager.register_handler(
            AsyncEventType.EVENT_UPLOAD_BILI,
            upload_event_handler.handle_bili_upload_event,
            priority=1
        )
        
        # 可以在这里添加更多事件处理器
        logger.info("事件处理器设置完成")
    
    async def _start_live_rooms(self):
        """启动所有直播间"""
        logger.info("开始启动直播间...")
        
        try:
            # 获取所有启用的直播间
            room_data_list = await run_blocking(DB.list_active_rooms)
            logger.info(f"发现 {len(room_data_list)} 个启用的直播间")
            
            # 批量启动直播间
            startup_semaphore = asyncio.Semaphore(10)

            async def start_with_limit(room_data):
                async with startup_semaphore:
                    return await self._start_single_room(room_data)

            start_tasks = []
            for room_data in room_data_list:
                task = asyncio.create_task(start_with_limit(room_data))
                start_tasks.append(task)
            
            if start_tasks:
                results = await asyncio.gather(*start_tasks, return_exceptions=True)
                
                success_count = 0
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        logger.error(f"启动直播间失败 {room_data_list[i].get('room_name')}: {result}")
                    else:
                        success_count += 1
                
                logger.info(f"直播间启动完成: 成功 {success_count}/{len(room_data_list)}")
            
        except Exception as e:
            logger.error(f"启动直播间时发生错误: {e}")
    
    async def _start_single_room(self, room_data: Dict):
        """启动单个直播间"""
        room_id = str(room_data.get('id'))
        
        try:
            live_room = await start_room_runtime(room_data)
            self.live_rooms[room_id] = live_room
            
            logger.info(f"直播间启动成功: {room_data.get('room_name')}")
            
        except Exception as e:
            logger.error(f"启动直播间失败 {room_data.get('room_name')}: {e}")
            raise
    
    async def _stop_live_rooms(self):
        """停止所有直播间"""
        if not self.live_rooms:
            return
        
        logger.info(f"停止 {len(self.live_rooms)} 个直播间...")
        
        try:
            await async_live_room_manager.stop()
            self.live_rooms.clear()
            logger.info("所有直播间已停止")
            
        except Exception as e:
            logger.error(f"停止直播间时出错: {e}")
    
    async def _start_timers(self):
        """启动定时任务"""
        logger.info("启动定时任务...")
        
        # 异步清理任务
        cleanup_task = asyncio.create_task(
            self._periodic_cleanup(),
            name="periodic-cleanup"
        )
        self.timer_tasks.append(cleanup_task)

        # B站投稿账号登录态巡检任务
        account_check_task = asyncio.create_task(
            self._periodic_account_check(),
            name="periodic-account-check"
        )
        self.timer_tasks.append(account_check_task)

        # 直播间激活时段到期巡检任务
        room_expiry_task = asyncio.create_task(
            self._periodic_room_expiry_check(),
            name="periodic-room-expiry-check"
        )
        self.timer_tasks.append(room_expiry_task)

        daily_clip_flush_task = asyncio.create_task(
            self._periodic_daily_clip_flush(),
            name="periodic-daily-clip-flush"
        )
        self.timer_tasks.append(daily_clip_flush_task)

        logger.info("定时任务启动完成")
    
    async def _periodic_cleanup(self):
        """定期清理任务"""
        while self.running:
            try:
                if not self.running:
                    break
                
                logger.info("开始定期清理...")
                
                # 在线程中执行清理逻辑
                await run_blocking(self._cleanup_old_files)
                
                logger.info("定期清理完成")
                
            except asyncio.CancelledError:
                # 清理任务取消是正常关闭流程，不需要debug日志
                break
            except Exception as e:
                logger.error(f"定期清理失败: {e}")

            await asyncio.sleep(1800)  # 30分钟执行一次

    async def _periodic_daily_clip_flush(self):
        """启动后补扫一次，之后每 10 分钟冲刷跨天/已下播的舞蹈切片日结包。"""
        from luboman.core.dance_clip import flush_daily_bili_clip_batches

        await asyncio.sleep(60)
        while self.running:
            try:
                if not self.running:
                    break
                await flush_daily_bili_clip_batches()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("舞蹈切片日结冲刷失败")
            await asyncio.sleep(600)

    def _account_check_interval(self):
        """账号登录态巡检间隔（秒），可通过配置 bili_account_check_interval 覆盖，默认 6 小时。"""
        try:
            return max(300, int(config.get('bili_account_check_interval', 21600)))
        except (TypeError, ValueError):
            return 21600

    async def _periodic_account_check(self):
        """定期检查 B站投稿账号登录态，失效则通过通知插件告警。

        只在「由有效变失效」时告警一次，避免对同一个死账号周期性刷屏；
        账号恢复后自动移出告警集合，下次再次失效会重新告警。
        """
        # 启动后稍等再首次检查，避免与启动流程挤在一起
        await asyncio.sleep(30)
        while self.running:
            try:
                if not self.running:
                    break

                active_count, invalid = await run_blocking(bili_account_health.check_active_accounts)

                # 失效账号先尝试 token 续期;续期成功恢复的不再告警,仍失效的才提示重新扫码
                if invalid and config.get('biliup_renew_enabled', True):
                    invalid = await run_blocking(bili_account_health.renew_and_recheck, invalid)

                invalid_ids = {a.get('id') for a in invalid}

                newly_invalid = [a for a in invalid if a.get('id') not in self._alerted_account_ids]
                for acc in newly_invalid:
                    name = acc.get('account_name') or f"id={acc.get('id')}"
                    notify_message(
                        'B站投稿账号登录态失效',
                        f'账号「{name}」登录态已失效，请尽快重新获取 cookie，否则该账号的自动投稿会失败。'
                    )

                # 用本轮失效集合刷新去重：恢复的账号自动移出，可再次告警
                self._alerted_account_ids = invalid_ids

                logger.info(
                    f"B站账号登录态巡检完成: 启用账号 {active_count} 个，失效 {len(invalid)} 个"
                    + (f"，新增告警 {len(newly_invalid)} 个" if newly_invalid else "")
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"B站账号登录态巡检失败: {e}")

            await asyncio.sleep(self._account_check_interval())

    def _room_expiry_check_interval(self):
        """激活时段到期巡检间隔（秒），可通过配置 live_room_expiry_check_interval 覆盖，默认 60 秒。"""
        try:
            return max(10, int(config.get('live_room_expiry_check_interval', 60)))
        except (TypeError, ValueError):
            return 60

    async def _periodic_room_expiry_check(self):
        """定期把激活截止时间已过的直播间置为未激活并停止 worker。

        启动后立即先跑一轮：服务停机期间过期的房间会在本次启动后被及时停用，
        而不是等上一个 interval 才处理。
        """
        while self.running:
            try:
                if not self.running:
                    break

                expired_rooms = await run_blocking(DB.deactivate_expired_rooms)
                for room_data in expired_rooms:
                    logger.info(
                        f"直播间「{room_data.get('room_name')}」(id={room_data.get('id')}) "
                        f"激活截止时间 {room_data.get('active_end')} 已到，自动置为未激活"
                    )
                    await stop_room_runtime(room_data.get('id'))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"直播间激活到期巡检失败: {e}")

            await asyncio.sleep(self._room_expiry_check_interval())

    def _cleanup_old_files(self):
        """清理旧文件（同步版本）"""
        try:
            local_video_file_remain_days = int(config.get("local_video_file_remain_days", 3))
            stale_completed = DB.cleanup_stale_recording_files(
                config.get("record_file_stale_timeout_seconds", 3600)
            )
            if stale_completed:
                logger.info(f"修复了 {stale_completed} 条超时录制中文件记录")

            video_dir = get_video_dir()
            
            if not os.path.exists(video_dir):
                return
            
            current_time = datetime.datetime.now()
            cutoff_time = current_time - datetime.timedelta(days=local_video_file_remain_days)
            
            cleaned_count = 0
            for platform_dir in os.listdir(video_dir):
                platform_path = os.path.join(video_dir, platform_dir)
                if not os.path.isdir(platform_path):
                    continue
                
                for room_dir in os.listdir(platform_path):
                    room_path = os.path.join(platform_path, room_dir)
                    if not os.path.isdir(room_path):
                        continue
                    
                    for day_dir in os.listdir(room_path):
                        day_path = os.path.join(room_path, day_dir)
                        if not os.path.isdir(day_path):
                            continue
                        
                        try:
                            day_time = datetime.datetime.strptime(day_dir, "%Y-%m-%d")
                            if day_time < cutoff_time:
                                remove_dir(day_path)
                                # 配套清理该目录下的 RecordFile 死记录，避免列表里出现文件已删的幽灵条目
                                try:
                                    DB.delete_record_files_under_path(day_path)
                                except Exception as e:
                                    logger.error(f"清理录像记录失败: {day_path} {e}")
                                cleaned_count += 1
                        except ValueError:
                            continue
            
            if cleaned_count > 0:
                logger.info(f"清理了 {cleaned_count} 个过期目录")
                
        except Exception as e:
            logger.error(f"文件清理失败: {e}")
    
    async def _start_monitoring(self):
        """启动系统监控"""
        self.monitor_task = asyncio.create_task(
            self._system_monitor(),
            name="system-monitor"
        )
        logger.info("系统监控已启动")
    
    async def _system_monitor(self):
        """系统监控循环"""
        while self.running:
            try:
                await asyncio.sleep(60)  # 每分钟检查一次
                
                if not self.running:
                    break
                
                # 收集各组件统计信息
                stats = await self._collect_system_stats()
                
                # 检查系统健康状态
                await self._check_system_health(stats)
                
            except asyncio.CancelledError:
                # 系统监控取消是正常关闭流程，不需要debug日志
                break
            except Exception as e:
                logger.error(f"系统监控错误: {e}")
    
    async def _collect_system_stats(self) -> Dict:
        """收集系统统计信息"""
        stats = {
            'timestamp': time.time(),
            'running': self.running,
            'live_rooms_count': len(self.live_rooms)
        }
        
        # 收集各组件统计
        try:
            stats['event_manager'] = async_event_manager.get_stats()
            stats['network_manager'] = async_network_manager.get_stats()
            stats['database_manager'] = async_database_manager.get_stats()
            stats['upload_scheduler'] = async_upload_scheduler.get_stats()
            stats['live_room_manager'] = async_live_room_manager.get_stats()
        except Exception as e:
            logger.error(f"收集统计信息失败: {e}")
        
        return stats
    
    async def _check_system_health(self, stats: Dict):
        """检查系统健康状态"""
        try:
            # 检查事件管理器
            event_stats = stats.get('event_manager', {})
            queue_size = event_stats.get('queue_size', 0)
            
            if queue_size > 1000:
                logger.warning(f"事件队列积压严重: {queue_size}")
            elif queue_size > 500:
                logger.info(f"事件队列积压: {queue_size}")
            
            # 检查网络管理器
            network_stats = stats.get('network_manager', {})
            failed_rate = 0
            if network_stats.get('requests_total', 0) > 0:
                failed_rate = (network_stats.get('requests_failed', 0) / 
                             network_stats.get('requests_total', 1)) * 100
            
            if failed_rate > 20:
                logger.warning(f"网络请求失败率过高: {failed_rate:.1f}%")
            
            # 检查上传调度器
            upload_stats = stats.get('upload_scheduler', {})
            upload_queue_size = upload_stats.get('queue_size', 0)
            
            if upload_queue_size > 20:
                logger.warning(f"上传队列积压: {upload_queue_size}")
            
            # 定期报告整体状态
            if int(time.time()) % 300 == 0:  # 每5分钟报告一次
                self._log_system_status(stats)
                
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
    
    def _log_system_status(self, stats: Dict):
        """记录系统状态"""
        logger.info(
            f"系统状态报告 - "
            f"直播间: {stats.get('live_room_manager', {}).get('rooms_count', stats.get('live_rooms_count', 0))}, "
            f"事件队列: {stats.get('event_manager', {}).get('queue_size', 0)}, "
            f"网络成功率: {self._calculate_success_rate(stats.get('network_manager', {})):.1f}%, "
            f"上传队列: {stats.get('upload_scheduler', {}).get('queue_size', 0)}"
        )
    
    def _calculate_success_rate(self, network_stats: Dict) -> float:
        """计算网络成功率"""
        total = network_stats.get('requests_total', 0)
        success = network_stats.get('requests_success', 0)
        
        if total == 0:
            return 100.0
        
        return (success / total) * 100
    
    async def _start_web_service(self):
        """启动Web服务"""
        try:
            import luboman.web
            logger.info("启动Web服务...")
            
            # 在单独的任务中运行Web服务
            self.web_task = asyncio.create_task(
                luboman.web.serve(),
                name="web-service"
            )
            
            logger.info("Web服务启动完成")
            
        except Exception as e:
            logger.error(f"Web服务启动失败: {e}")
    
    def _cleanup_legacy_plugins(self):
        """清理传统插件"""
        try:
            logger.info(f"清理 {len(PluginTool.running_plugins)} 个传统插件")
            
            for room_id, plugin in list(PluginTool.running_plugins.items()):
                try:
                    plugin.stop()
                except Exception as e:
                    logger.error(f"停止传统插件 {room_id} 失败: {e}")
            
            PluginTool.running_plugins.clear()
            logger.info("传统插件清理完成")
            
        except Exception as e:
            logger.error(f"清理传统插件失败: {e}")


# 全局应用实例
app = AsyncLubomanApplication()


async def main():
    """异步主函数"""
    # 初始化日志和数据库
    logging.config.dictConfig(LOG_CONF)
    DB.init()
    config.load_from_db()
    PluginTool(plugins)
    
    # 设置信号处理
    def signal_handler(signum, frame):
        logger.info(f"接收到信号 {signum}，开始关闭...")
        asyncio.create_task(app.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 注册退出清理
    atexit.register(lambda: asyncio.run(app.stop()) if app.running else None)
    
    try:
        # 启动应用
        await app.start()
        
        # 等待应用运行
        while app.running:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("接收到键盘中断")
    except Exception as e:
        logger.error(f"应用运行错误: {e}")
    finally:
        await app.stop()


if __name__ == '__main__':
    # 设置异步事件循环策略
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    # 运行异步主程序
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("应用程序被用户中断")
    except Exception as e:
        logger.error(f"应用程序异常退出: {e}")
        sys.exit(1)
    else:
        logger.info("应用程序正常退出")
        sys.exit(0)
