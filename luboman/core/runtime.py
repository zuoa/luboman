import logging
import re
import time

from luboman.core.async_utils import run_blocking
from luboman.core.decorators import PluginTool
from luboman.core.event import Event, EventType

logger = logging.getLogger('luboman')


def collect_runtime_stats():
    """Collect sync and async runtime state for API responses and diagnostics."""
    from luboman.core.thread_pool import thread_pool_manager
    from luboman.core.async_event import async_event_manager
    from luboman.core.async_network import async_network_manager
    from luboman.core.async_database import async_database_manager
    from luboman.core.async_upload import async_upload_scheduler
    from luboman.core.async_live import async_live_room_manager

    return {
        "timestamp": time.time(),
        "running_plugins_count": len(PluginTool.running_plugins),
        "running_plugin_ids": list(PluginTool.running_plugins.keys()),
        "thread_pool": thread_pool_manager.get_stats(),
        "async": {
            "event_manager": async_event_manager.get_stats(),
            "network_manager": async_network_manager.get_stats(),
            "database_manager": async_database_manager.get_stats(),
            "upload_scheduler": async_upload_scheduler.get_stats(),
            "live_room_manager": async_live_room_manager.get_stats(),
        },
    }


async def start_room_runtime(room_data):
    """Start a room through the active runtime mode."""
    from luboman.core.async_live import async_live_room_manager, AsyncLiveBase

    room_id = str(room_data.get("id", ""))
    if async_live_room_manager.running:
        if room_id in async_live_room_manager.live_rooms:
            return async_live_room_manager.live_rooms[room_id]

        room_url = room_data.get("room_url", "")
        for plugin in PluginTool.live_plugins:
            if re.match(plugin.VALID_URL_BASE, room_url):
                suffix = room_data.get("stream_video_format") or "flv"
                plugin_instance = plugin(room_data.get("room_name"), room_url, suffix)
                plugin_instance.room_data = room_data
                live_room = AsyncLiveBase(plugin_instance)
                await async_live_room_manager.add_room(live_room)
                return live_room

        raise ValueError(f"未找到匹配的插件: {room_url}")

    from luboman.core.live import start_room

    return await run_blocking(start_room, room_data, **{})


async def stop_room_runtime(room_id):
    """Stop a room through the active runtime mode."""
    from luboman.core.async_live import async_live_room_manager

    room_id = str(room_id)
    if async_live_room_manager.running:
        await async_live_room_manager.remove_room(room_id)
        return room_id

    from luboman.core.live import stop_room

    await run_blocking(stop_room, room_id)
    return room_id


async def refresh_room_runtime(room_data):
    """Push updated room config into a running plugin instance."""
    from luboman.core.async_live import async_live_room_manager

    room_id = str(room_data.get("id", ""))
    if async_live_room_manager.running and room_id in async_live_room_manager.live_rooms:
        # 异步模式下直接更新共享的 room_data：AsyncLiveBase.room_data 与 plugin_instance.room_data
        # 是同一个字典对象，且异步事件总线未注册 EVENT_REFRESH_ROOM_INFO 处理器时，发事件会变成 no-op。
        live_room = async_live_room_manager.live_rooms[room_id]
        if isinstance(room_data, dict):
            live_room.room_data.update(room_data)
            live_room.plugin_instance.room_data = live_room.room_data
        return

    running_plugin = PluginTool.running_plugins.get(room_id)
    if running_plugin:
        running_plugin.send_event(Event(EventType.EVENT_REFRESH_ROOM_INFO, (room_data,)))


def _is_room_active(room_data):
    try:
        return int((room_data or {}).get("active_state", 0) or 0) == 1
    except (TypeError, ValueError):
        return False


def _is_room_running(room_id):
    from luboman.core.async_live import async_live_room_manager

    if async_live_room_manager.running and room_id in async_live_room_manager.live_rooms:
        return True
    return room_id in PluginTool.running_plugins


async def reconcile_room_runtime(room_data):
    """页面切换激活状态后，让 worker 运行态与 active_state 保持一致。

    - 激活且未运行 -> 启动
    - 未激活且运行中 -> 停止
    - 激活且运行中   -> 仅刷新配置
    """
    room_id = str(room_data.get("id", ""))
    active = _is_room_active(room_data)
    running = _is_room_running(room_id)

    if active and not running:
        logger.info(f"激活直播间 {room_id}，启动 worker")
        await start_room_runtime(room_data)
    elif not active and running:
        logger.info(f"停用直播间 {room_id}，停止 worker")
        await stop_room_runtime(room_id)
    elif active and running:
        await refresh_room_runtime(room_data)

    return room_id
