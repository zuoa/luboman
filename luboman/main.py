import asyncio
import atexit
import datetime
import functools
import logging.config
import os
import signal
import sys

from playhouse.shortcuts import model_to_dict

import luboman.web
from luboman.config import config
from luboman.core import bili_account_health
from luboman.core.decorators import PluginTool
from luboman.core.live import start_room
from luboman.core.notify import notify_message
from luboman.core.timer import Timer
from luboman import LOG_CONF
from luboman.core.utils import get_video_dir, remove_dir
from luboman.database.models import LiveRoom
from luboman.database.db import DB

from luboman import plugins
from luboman.core.thread_pool import thread_pool_manager

logger = logging.getLogger("luboman")
_alerted_account_ids = set()


async def start_all_record():
    for room in LiveRoom.select().where(LiveRoom.active_state == 1):
        start_room(model_to_dict(room), **{})


def check_runtime_state():
    logger.info("检查运行状态")
    try:
        local_video_file_remain_days = int(config.get("local_video_file_remain_days", 3))
        stale_completed = DB.cleanup_stale_recording_files(
            config.get("record_file_stale_timeout_seconds", 3600)
        )
        if stale_completed:
            logger.info(f"修复了 {stale_completed} 条超时录制中文件记录")

        video_dir = get_video_dir()

        for platform_dir in os.listdir(video_dir):
            if not os.path.isdir(os.path.join(video_dir, platform_dir)):
                continue

            for room_dir in os.listdir(os.path.join(video_dir, platform_dir)):
                if not os.path.isdir(os.path.join(video_dir, platform_dir, room_dir)):
                    continue

                for day_dir in os.listdir(os.path.join(video_dir, platform_dir, room_dir)):
                    if not os.path.isdir(os.path.join(video_dir, platform_dir, room_dir, day_dir)):
                        continue
                    day_time = datetime.datetime.strptime(day_dir, "%Y-%m-%d")
                    if (day_time + datetime.timedelta(days=local_video_file_remain_days)) < datetime.datetime.now():
                        remove_dir(os.path.join(video_dir, platform_dir, room_dir, day_dir))
    except Exception as e:
        logger.error(f"检查运行状态失败: {e}")


def cleanup_resources():
    """清理所有资源"""
    logger.info("开始清理应用资源...")
    
    try:
        # 清理运行中的插件
        from luboman.core.decorators import PluginTool
        logger.info(f"停止 {len(PluginTool.running_plugins)} 个运行中的直播间插件")
        for room_id, plugin in list(PluginTool.running_plugins.items()):
            try:
                logger.debug(f"停止直播间插件: {room_id}")
                plugin.stop()
            except Exception as e:
                logger.error(f"停止插件 {room_id} 时出错: {e}")
        
        PluginTool.running_plugins.clear()
        logger.info("直播间插件清理完成")
        
        # 清理全局线程池
        logger.info("清理全局线程池...")
        thread_pool_manager.shutdown(wait=True, timeout=30)
        
        logger.info("资源清理完成")
    except Exception as e:
        logger.error(f"清理资源时出错: {e}")

def signal_handler(signum, frame):
    """信号处理函数"""
    logger.info(f"接收到信号 {signum}，开始退出...")
    cleanup_resources()
    sys.exit(0)

def do_exit(lp):
    cleanup_resources()
    lp.stop()


def monitor_thread_pools():
    """监控线程池状态"""
    try:
        stats = thread_pool_manager.get_stats()
        logger.info(f"线程池状态: {stats}")
        
        # 检查线程池健康状态
        if not thread_pool_manager.is_healthy():
            logger.warning("线程池健康检查失败")
        
        # 如果待处理任务过多，发出警告
        for pool_name, stat in stats.items():
            if stat['pending_tasks'] > 50:
                logger.warning(f"线程池 {pool_name} 待处理任务过多: {stat['pending_tasks']}")
    except Exception as e:
        logger.error(f"监控线程池时出错: {e}")


def account_check_interval():
    try:
        return max(300, int(config.get('bili_account_check_interval', 21600)))
    except (TypeError, ValueError):
        return 21600


def check_bili_account_login_state():
    global _alerted_account_ids
    try:
        active_count, invalid = bili_account_health.check_active_accounts()
        invalid_ids = {account.get('id') for account in invalid}
        newly_invalid = [account for account in invalid if account.get('id') not in _alerted_account_ids]

        for account in newly_invalid:
            name = account.get('account_name') or f"id={account.get('id')}"
            notify_message(
                'B站投稿账号登录态失效',
                f'账号「{name}」登录态已失效，请尽快重新获取 cookie，否则该账号的自动投稿会失败。'
            )

        _alerted_account_ids = invalid_ids
        logger.info(
            f"B站账号登录态巡检完成: 启用账号 {active_count} 个，失效 {len(invalid)} 个"
            + (f"，新增告警 {len(newly_invalid)} 个" if newly_invalid else "")
        )
    except Exception as e:
        logger.error(f"B站账号登录态巡检失败: {e}")

if __name__ == '__main__':
    # 初始化日志和数据库
    logging.config.dictConfig(LOG_CONF)
    DB.init()
    config.load_from_db()
    PluginTool(plugins)

    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 注册退出清理函数
    atexit.register(cleanup_resources)
    
    # 启动定时任务
    Timer(func=check_runtime_state, interval=1800).start()  # 清理本地文件
    Timer(func=monitor_thread_pools, interval=300).start()  # 监控线程池
    Timer(func=check_bili_account_login_state, interval=account_check_interval()).start()  # B站投稿账号登录态巡检
    
    logger.info("应用初始化完成，开始运行...")
    
    try:
        loop = asyncio.get_event_loop()
        future = asyncio.gather(start_all_record(), luboman.web.serve())
        future.add_done_callback(functools.partial(do_exit, loop))
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("接收到中断信号")
    except Exception as e:
        logger.error(f"应用运行出错: {e}")
    finally:
        cleanup_resources()
        loop.close()
        logger.info("应用已退出")
