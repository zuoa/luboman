import asyncio
import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from luboman.core.async_event import AsyncEvent, AsyncEventManager
from luboman.core.async_network import AsyncNetworkManager, NetworkRequest, NetworkResponse
from luboman.core.async_utils import run_blocking
from luboman.core.async_upload import AsyncUploadScheduler, AsyncUploadTask, UploadPriority, UploadResult

try:
    from luboman.core.async_database import AsyncDatabaseManager, DatabaseOperation
    from luboman.database.db import DB
except ModuleNotFoundError:
    AsyncDatabaseManager = None
    DatabaseOperation = None
    DB = None

try:
    import luboman.web as web_module
except ModuleNotFoundError:
    web_module = None


class AsyncEventManagerRefactorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await asyncio.sleep(0)

    async def test_room_scoped_handlers_only_receive_matching_events(self):
        manager = AsyncEventManager(worker_count=1, queue_size=10)
        received = []

        async def handler(event):
            received.append(event.room_id)

        manager.register_handler("status", handler, room_id="room-1")
        await manager.start()
        try:
            await manager.send_event(AsyncEvent("status", room_id="room-2"))
            await manager.send_event(AsyncEvent("status", room_id="room-1"))
            await asyncio.wait_for(manager.event_queue.join(), timeout=2)
        finally:
            await manager.stop()

        self.assertEqual(received, ["room-1"])

    async def test_sync_handler_runs_without_create_task_type_error(self):
        manager = AsyncEventManager(worker_count=1, queue_size=10)
        received = []

        def handler(event):
            received.append(event.type_)

        manager.register_handler("sync", handler)
        await manager.start()
        try:
            await manager.send_event(AsyncEvent("sync"))
            await asyncio.wait_for(manager.event_queue.join(), timeout=2)
        finally:
            await manager.stop()

        self.assertEqual(received, ["sync"])


class AsyncUploadSchedulerRefactorTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_priority_tasks_are_queueable(self):
        scheduler = AsyncUploadScheduler(max_concurrent_uploads=1)
        scheduler.running = True
        try:
            await scheduler.schedule_upload(AsyncUploadTask(platform="local", priority=UploadPriority.NORMAL))
            await scheduler.schedule_upload(AsyncUploadTask(platform="local", priority=UploadPriority.NORMAL))

            status = await scheduler.get_queue_status()
        finally:
            scheduler.running = False

        self.assertEqual(status["queue_size"], 2)
        self.assertEqual(status["priority_distribution"]["NORMAL"], 2)

    async def test_perform_upload_passes_room_data_as_single_context(self):
        import luboman.core.upload as upload_module

        scheduler = AsyncUploadScheduler(max_concurrent_uploads=1)
        calls = []
        original_upload = upload_module.upload

        def fake_upload(platform, file_list, **kwargs):
            calls.append((platform, file_list, kwargs))
            return True

        upload_module.upload = fake_upload
        try:
            task = AsyncUploadTask(
                platform="biliup-rs",
                file_list=[{"video": "/tmp/not-exist.flv"}],
                room_data={"id": 1, "room_title": "title"},
            )
            success, uploaded_files, failed_files, error = await scheduler._perform_upload(task)
        finally:
            upload_module.upload = original_upload

        self.assertTrue(success)
        self.assertEqual(calls[0][0], "biliup-rs")
        self.assertEqual(calls[0][2], {"room_data": {"id": 1, "room_title": "title"}})
        self.assertEqual(uploaded_files, ["/tmp/not-exist.flv"])
        self.assertEqual(failed_files, [])
        self.assertIsNone(error)

    async def test_stop_cancels_pending_retry_tasks(self):
        scheduler = AsyncUploadScheduler(max_concurrent_uploads=1)
        scheduler.running = True

        upload_task = AsyncUploadTask(platform="local", max_retries=1)
        await scheduler._handle_upload_result(
            upload_task,
            UploadResult(task_id=upload_task.task_id, success=False, platform="local", error_message="failed"),
        )

        self.assertEqual(len(scheduler.retry_tasks), 1)
        await scheduler.stop()

        self.assertFalse(scheduler.retry_tasks)
        self.assertEqual(scheduler.upload_queue.qsize(), 0)

    def test_upload_filters_kwargs_by_plugin_signature(self):
        from luboman.core.decorators import PluginTool
        from luboman.core.upload import upload

        class StorageUploader:
            received = None

            def __init__(self, file_list):
                self.file_list = file_list

            def start(self):
                StorageUploader.received = self.file_list
                return True

        class BiliUploader:
            received = None

            def __init__(self, file_list, room_data):
                self.file_list = file_list
                self.room_data = room_data

            def start(self):
                BiliUploader.received = self.room_data
                return True

        original_plugins = PluginTool.upload_plugins.copy()
        PluginTool.upload_plugins["storage-test"] = StorageUploader
        PluginTool.upload_plugins["bili-test"] = BiliUploader
        try:
            self.assertTrue(upload("storage-test", [{"video": "a"}], room_data={"id": 1}, ignored=True))
            self.assertTrue(upload("bili-test", [{"video": "b"}], room_data={"id": 2}, ignored=True))
        finally:
            PluginTool.upload_plugins = original_plugins

        self.assertEqual(StorageUploader.received, [{"video": "a"}])
        self.assertEqual(BiliUploader.received, {"id": 2})


class AsyncUtilityRefactorTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_blocking_executes_sync_callable_with_kwargs(self):
        main_thread_id = threading.get_ident()
        worker_thread_ids = []

        def sync_work(value, increment=0):
            worker_thread_ids.append(threading.get_ident())
            return value + increment

        result = await run_blocking(sync_work, 2, increment=3)

        self.assertEqual(result, 5)
        self.assertTrue(worker_thread_ids)
        self.assertNotEqual(worker_thread_ids[0], main_thread_id)


class AsyncNetworkManagerRefactorTest(unittest.IsolatedAsyncioTestCase):
    class _Content:
        def __init__(self, data):
            self.data = data

        async def read(self, size):
            return self.data[:size]

    class _Response:
        def __init__(self, data, headers=None, charset="utf-8"):
            self.headers = headers or {}
            self.content = AsyncNetworkManagerRefactorTest._Content(data)
            self.charset = charset

    async def test_limited_response_reader_decodes_json(self):
        manager = AsyncNetworkManager()
        response = self._Response(b'{"ok": true}', {"Content-Type": "application/json"})

        data = await manager._read_response_data(
            response,
            NetworkRequest(url="https://example.test"),
            "application/json",
        )

        self.assertEqual(data, {"ok": True})

    async def test_limited_response_reader_rejects_large_payloads(self):
        manager = AsyncNetworkManager()
        manager.max_response_payload_size = 4
        response = self._Response(b"12345", {"Content-Length": "5"})

        with self.assertRaises(ValueError):
            await manager._read_limited_response_bytes(response)

    async def test_batch_requests_limits_in_flight_tasks_and_preserves_input_order(self):
        class FakeNetworkManager(AsyncNetworkManager):
            def __init__(self):
                super().__init__(max_concurrent=2)
                self.active = 0
                self.max_active = 0
                self.started = []

            async def single_request(self, request):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.started.append(request.room_id)
                try:
                    await asyncio.sleep(0.01)
                    return NetworkResponse(
                        success=True,
                        data=request.room_id,
                        room_id=request.room_id,
                        request_type=request.request_type,
                    )
                finally:
                    self.active -= 1

        manager = FakeNetworkManager()
        requests = [
            NetworkRequest(url="https://example.test/3", room_id="3", priority=3),
            NetworkRequest(url="https://example.test/1", room_id="1", priority=1),
            NetworkRequest(url="https://example.test/2", room_id="2", priority=2),
            NetworkRequest(url="https://example.test/0", room_id="0", priority=0),
            NetworkRequest(url="https://example.test/4", room_id="4", priority=4),
        ]

        responses = await manager.batch_requests(requests)

        self.assertEqual([response.data for response in responses], ["3", "1", "2", "0", "4"])
        self.assertLessEqual(manager.max_active, 2)
        self.assertEqual(manager.started[:2], ["0", "1"])


@unittest.skipIf(web_module is None, "web dependencies are not installed")
class WebApiRefactorTest(unittest.IsolatedAsyncioTestCase):
    class _Request:
        def __init__(self, payload):
            self.payload = payload

        async def json(self):
            return dict(self.payload)

    def _response_json(self, response):
        return json.loads(response.text)

    async def test_live_room_add_uses_runtime_and_returns_created_id(self):
        original_run_db = web_module.run_db
        original_reconcile_room_runtime = web_module.reconcile_room_runtime
        calls = []

        async def fake_run_db(func, *args, **kwargs):
            calls.append(("run_db", func.__name__, args))
            self.assertEqual(func.__name__, "_create_live_room")
            return {"id": 12, "room_name": args[0]["room_name"], "room_url": args[0]["room_url"]}

        async def fake_reconcile_room_runtime(room_data):
            calls.append(("reconcile", room_data))

        web_module.run_db = fake_run_db
        web_module.reconcile_room_runtime = fake_reconcile_room_runtime
        try:
            response = await web_module.add_room(self._Request({"room_name": " room ", "room_url": " url "}))
        finally:
            web_module.run_db = original_run_db
            web_module.reconcile_room_runtime = original_reconcile_room_runtime

        data = self._response_json(response)
        self.assertTrue(data["success"])
        self.assertEqual(data["data"], 12)
        self.assertEqual(calls[0][2][0]["room_name"], "room")
        self.assertEqual(calls[0][2][0]["room_url"], "url")
        self.assertEqual(calls[1], ("reconcile", {"id": 12, "room_name": "room", "room_url": "url"}))

    async def test_bili_account_update_route_uses_db_executor(self):
        original_run_db = web_module.run_db
        calls = []

        async def fake_run_db(func, *args, **kwargs):
            calls.append((func.__name__, args))
            return 1

        web_module.run_db = fake_run_db
        try:
            response = await web_module.update_bili_account(self._Request({"id": 9, "state_active": 1}))
        finally:
            web_module.run_db = original_run_db

        data = self._response_json(response)
        self.assertTrue(data["success"])
        self.assertEqual(data["data"], 1)
        self.assertEqual(calls, [("_update_bili_account", ({"id": 9, "state_active": 1},))])


@unittest.skipIf(AsyncDatabaseManager is None, "database dependencies are not installed")
class AsyncDatabaseManagerRefactorTest(unittest.TestCase):
    def test_live_room_updates_are_merged_before_batch_write(self):
        manager = AsyncDatabaseManager()
        original = DB.batch_update_live_rooms
        calls = []

        def fake_batch_update(room_data_list):
            calls.append(room_data_list)
            return len(room_data_list)

        DB.batch_update_live_rooms = fake_batch_update
        try:
            operations = [
                DatabaseOperation("update", "live_room", {"id": 1, "room_title": "A"}, room_id="1"),
                DatabaseOperation("update", "live_room", {"id": 1, "live_state": 1}, room_id="1"),
                DatabaseOperation("update", "live_room", {"id": 2, "room_title": "B"}, room_id="2"),
            ]

            results = manager._batch_update_live_rooms(operations)
        finally:
            DB.batch_update_live_rooms = original

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 2)
        self.assertEqual(calls[0][0]["id"], 1)
        self.assertEqual(calls[0][0]["room_title"], "A")
        self.assertEqual(calls[0][0]["live_state"], 1)
        self.assertTrue(all(result.success for result in results))


@unittest.skipIf(DB is None, "database dependencies are not installed")
class DatabaseHelperIntegrationTest(unittest.TestCase):
    def setUp(self):
        from peewee import SqliteDatabase
        import luboman.database.db as db_module
        from luboman.database.models import (
            BiliAccount,
            BiliUploadTemplate,
            GlobalConfig,
            LiveRoom,
            RecordFile,
        )

        self.db_module = db_module
        self.original_db = db_module.db
        self.models = [GlobalConfig, LiveRoom, BiliAccount, BiliUploadTemplate, RecordFile]
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.db_path = tmp.name
        tmp.close()
        self.test_db = SqliteDatabase(self.db_path)
        self.bind_ctx = self.test_db.bind_ctx(self.models)
        self.bind_ctx.__enter__()
        db_module.db = self.test_db
        self.test_db.create_tables(self.models)

    def tearDown(self):
        self.test_db.drop_tables(self.models)
        self.db_module.db = self.original_db
        self.bind_ctx.__exit__(None, None, None)
        self.test_db.close()
        os.unlink(self.db_path)

    def test_db_helpers_filter_fields_and_run_real_crud(self):
        DB.init()

        room = DB.create_live_room({
            "room_url": "https://example.test/live",
            "room_name": "Example",
            "active_state": 1,
            "ffmpeg_options": {"copy": True},
            "ignored": "drop-me",
        })
        DB.create_live_room({
            "room_url": "https://example.test/off",
            "room_name": "Off",
            "active_state": 0,
        })

        self.assertEqual(room["room_name"], "Example")
        self.assertNotIn("ignored", room)
        self.assertEqual([item["id"] for item in DB.list_active_rooms()], [room["id"]])

        self.assertEqual(DB.update_live_room({"id": room["id"], "room_name": "Renamed", "ignored": "x"}), 1)
        self.assertEqual(DB.get_live_room_data(room["id"])["room_name"], "Renamed")

        # 激活时段到期自动置为未激活：active_end 为空永久激活，已过期则停用
        import datetime as _dt
        now = _dt.datetime.now()
        permanent = DB.create_live_room({
            "room_url": "https://example.test/permanent",
            "room_name": "Permanent",
            "active_state": 1,
        })
        future = DB.create_live_room({
            "room_url": "https://example.test/future",
            "room_name": "Future",
            "active_state": 1,
            "active_end": now + _dt.timedelta(hours=1),
        })
        expired_room = DB.create_live_room({
            "room_url": "https://example.test/expired",
            "room_name": "Expired",
            "active_state": 1,
            "active_end": now - _dt.timedelta(hours=1),
        })

        deactivated = DB.deactivate_expired_rooms(now)
        self.assertEqual([item["id"] for item in deactivated], [expired_room["id"]])
        self.assertEqual(deactivated[0]["active_state"], 0)
        self.assertEqual(DB.get_live_room_data(expired_room["id"])["active_state"], 0)
        self.assertEqual(DB.get_live_room_data(permanent["id"])["active_state"], 1)
        self.assertEqual(DB.get_live_room_data(future["id"])["active_state"], 1)
        # 已停用的房间不会重复返回
        self.assertEqual(DB.deactivate_expired_rooms(now), [])

        DB.delete_live_room(permanent["id"])
        DB.delete_live_room(future["id"])
        DB.delete_live_room(expired_room["id"])

        account = DB.create_bili_account({
            "account_name": "Uploader",
            "bili_cookies": "SESSDATA=x;",
            "ignored": "drop-me",
        })
        self.assertEqual(account["account_name"], "Uploader")
        self.assertEqual(DB.update_bili_account({"id": account["id"], "state_active": 0, "ignored": "x"}), 1)
        self.assertEqual(DB.list_bili_account()[0]["state_active"], 0)

        template_id = DB.create_bili_upload_template({
            "template_name": "Default",
            "bili_account_id": account["id"],
            "tags": ["录播Man"],
            "ignored": "drop-me",
        })
        self.assertEqual(DB.update_bili_upload_template({"id": template_id, "title": "Title", "ignored": "x"}), 1)
        self.assertEqual(DB.list_bili_upload_template()[0]["title"], "Title")
        self.assertEqual(DB.delete_bili_upload_template(template_id), 1)
        self.assertEqual(DB.delete_live_room(room["id"]), 1)


class DeploymentRefactorTest(unittest.TestCase):
    def test_docker_uses_async_entrypoint(self):
        dockerfile = Path(__file__).with_name("Dockerfile").read_text(encoding="utf-8")
        self.assertIn('ENTRYPOINT ["python", "async_main.py"]', dockerfile)

    def test_async_main_uses_core_runtime_helpers(self):
        source = Path(__file__).with_name("luboman").joinpath("async_main.py").read_text(encoding="utf-8")

        self.assertIn("from luboman.core.runtime import start_room_runtime", source)
        self.assertNotIn("from luboman.web import start_room_runtime", source)

    def test_async_main_loads_rooms_off_event_loop_with_bounded_startup(self):
        source = Path(__file__).with_name("luboman").joinpath("async_main.py").read_text(encoding="utf-8")

        self.assertIn("room_data_list = await run_blocking(DB.list_active_rooms)", source)
        self.assertIn("startup_semaphore = asyncio.Semaphore(10)", source)
        self.assertNotIn("LiveRoom.select()", source)
        self.assertNotIn("model_to_dict(room)", source)

    def test_web_exposes_bili_account_update_route(self):
        source = Path(__file__).with_name("luboman").joinpath("web", "__init__.py").read_text(encoding="utf-8")

        self.assertIn('@routes.post("/v1/BiliAccount/update")', source)
        self.assertIn("await run_db(_update_bili_account, data)", source)

    def test_web_crud_uses_db_helpers(self):
        source = Path(__file__).with_name("luboman").joinpath("web", "__init__.py").read_text(encoding="utf-8")

        self.assertIn("return DB.create_live_room(data)", source)
        self.assertIn("return DB.create_bili_account(payload)", source)
        self.assertIn("return DB.create_bili_upload_template(data)", source)
        self.assertNotIn("LiveRoom.create", source)
        self.assertNotIn("BiliAccount.create", source)
        self.assertNotIn("BiliUploadTemplate.create", source)

    def test_db_init_is_idempotent_and_create_helpers_filter_fields(self):
        source = Path(__file__).with_name("luboman").joinpath("database", "db.py").read_text(encoding="utf-8")

        self.assertIn("create_table(safe=True)", source)
        self.assertIn("def filter_model_data", source)
        self.assertIn("LiveRoom.create(**cls.filter_model_data(LiveRoom, data))", source)
        self.assertIn("BiliAccount.create(**cls.filter_model_data(BiliAccount, data))", source)
        self.assertIn("BiliUploadTemplate.create(**cls.filter_model_data(BiliUploadTemplate, data))", source)


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _swap_database_to_sqlite():
    """把所有模型临时绑定到临时文件 SQLite 并替换 db 全局引用，返回恢复函数。

    用文件而非 :memory:，因为发布流程通过 run_db 在线程池里执行 DB 操作，
    而每个线程对 :memory: 都会得到一个空的全新库；文件库则可被所有线程共享。
    DB 类与 BaseModel 的辅助方法都通过各自模块里的 db 全局调用
    connection_context()，因此需要同时替换 models.db 与 db.db 两个引用。
    """
    from playhouse.sqlite_ext import SqliteExtDatabase
    from luboman.database import models as models_module
    from luboman.database import db as db_module
    from luboman.database.models import (
        GlobalConfig, LiveRoom, BiliAccount, BiliUploadTemplate, RecordFile,
    )

    db_path = tempfile.mktemp(suffix='.db')
    test_db = SqliteExtDatabase(db_path)
    original = (models_module.db, db_module.db)
    models_module.db = test_db
    db_module.db = test_db
    models = [GlobalConfig, LiveRoom, BiliAccount, BiliUploadTemplate, RecordFile]
    for model in models:
        model.bind(test_db)
    test_db.connect(reuse_if_open=True)
    test_db.create_tables(models)

    def restore():
        test_db.close()
        for model in models:
            model.bind(original[0])
        models_module.db = original[0]
        db_module.db = original[1]
        try:
            os.remove(db_path)
        except OSError:
            pass

    return restore


class RecordFileDatabaseHelperTest(unittest.TestCase):
    """DB.list_record_file 字段、过滤与分页。"""

    def setUp(self):
        self._restore_db = _swap_database_to_sqlite()

    def tearDown(self):
        self._restore_db()

    def _create_room_and_records(self):
        from luboman.database.models import LiveRoom, RecordFile

        room = LiveRoom.create(room_url='http://t/1', room_name='streamerA', room_platform='douyin')
        other = LiveRoom.create(room_url='http://t/2', room_name='streamerB', room_platform='bilibili')
        now = datetime.datetime.now()
        RecordFile.create(live_room_id=room.id, begin_time=now, end_time=now,
                          video='/data/video/douyin/1-streamerA/2026-01-01/a.flv')
        RecordFile.create(live_room_id=room.id, begin_time=now, end_time=now,
                          video='/data/video/douyin/1-streamerA/2026-01-02/b.flv')
        RecordFile.create(live_room_id=other.id, begin_time=now, end_time=now,
                          video='/data/video/bilibili/2-streamerB/2026-01-01/c.flv')
        return room

    def test_list_returns_all_with_expected_fields(self):
        from luboman.database.db import DB

        self._create_room_and_records()
        records, total = DB.list_record_file()

        self.assertEqual(total, 3)
        self.assertEqual(len(records), 3)
        for field in ('id', 'video', 'begin_time', 'end_time', 'series_code',
                      'upload_info', 'room_name', 'room_platform'):
            self.assertIn(field, records[0])

    def test_list_merges_room_name_and_platform(self):
        from luboman.database.db import DB

        room = self._create_room_and_records()
        records, _ = DB.list_record_file()

        room_records = [r for r in records if r['live_room_id'] == room.id]
        self.assertEqual(len(room_records), 2)
        for record in room_records:
            self.assertEqual(record['room_name'], 'streamerA')
            self.assertEqual(record['room_platform'], 'douyin')

    def test_list_filters_by_live_room_id(self):
        from luboman.database.db import DB

        room = self._create_room_and_records()
        records, total = DB.list_record_file({'live_room_id': room.id})

        self.assertEqual(total, 2)
        self.assertTrue(all(r['live_room_id'] == room.id for r in records))

    def test_list_paginates(self):
        from luboman.database.db import DB

        room = self._create_room_and_records()
        page1, total = DB.list_record_file({'live_room_id': room.id}, page=1, page_size=1)
        page2, _ = DB.list_record_file({'live_room_id': room.id}, page=2, page_size=1)

        self.assertEqual(total, 2)
        self.assertEqual(len(page1), 1)
        self.assertEqual(len(page2), 1)
        self.assertNotEqual(page1[0]['id'], page2[0]['id'])


class RecordFileListDbDrivenTest(unittest.TestCase):
    """列表为纯数据库驱动：直接展示 DB 记录，按 exists_only 口径补磁盘状态；
    exists_only 默认隐藏磁盘已不存在的记录，且 total 等于实际可见文件数。"""

    def _record(self, video, **extra):
        record = {
            'id': 1, 'live_room_id': 1, 'video': video,
            '_video_real': os.path.realpath(video),
            'begin_time': None, 'end_time': None,
            'series_code': None, 'upload_info': None,
            'room_name': 'streamerA', 'room_platform': 'douyin',
        }
        record.update(extra)
        return record

    def _patch_list(self, web, records):
        return patch.object(web.DB, 'list_record_file', return_value=(records, len(records)))

    def test_returns_db_record_with_disk_info(self):
        import luboman.web as web
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, 'tracked.flv')
            with open(video, 'wb') as fh:
                fh.write(b'x' * 100)
            with self._patch_list(web, [self._record(video)]):
                entries, total, _ = web._list_record_files_data({})
            self.assertEqual(total, 1)
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry['source'], 'database')
            self.assertTrue(entry['exists'])
            self.assertEqual(entry['size'], 100)
            self.assertEqual(entry['id'], 1)

    def test_exists_only_hides_missing_db_records_by_default(self):
        import luboman.web as web
        with tempfile.TemporaryDirectory() as tmp:
            ghost = os.path.join(tmp, 'ghost.flv')  # 数据库有记录但磁盘不存在
            records = [self._record(ghost, id=9)]
            with self._patch_list(web, records):
                # exists_only 默认 True：stat 判 exists=False 后过滤掉，total 也按可见文件重算
                entries, total, _ = web._list_record_files_data({})
            self.assertEqual(total, 0)
            self.assertEqual(entries, [])

            with self._patch_list(web, records):
                entries, total, _ = web._list_record_files_data({'exists_only': False})
            self.assertEqual(total, 1)
            self.assertFalse(entries[0]['exists'])

    def test_disk_only_files_not_returned(self):
        """行为变化：磁盘上有、但未入库的文件不再出现在列表（列表改为纯 DB 驱动）。"""
        import luboman.web as web
        with tempfile.TemporaryDirectory() as tmp:
            extra = os.path.join(tmp, 'extra.flv')  # 仅在磁盘、未入库
            with open(extra, 'wb') as fh:
                fh.write(b'x' * 10)
            with self._patch_list(web, []):  # DB 无记录
                entries, total, _ = web._list_record_files_data({})
            self.assertEqual(total, 0)
            self.assertEqual(entries, [])

    def test_passes_filters_and_pagination_to_db(self):
        import luboman.web as web
        with patch.object(web.DB, 'list_record_file', return_value=([], 0)) as mocked:
            web._list_record_files_data({
                'live_room_id': 7,
                'page': 2,
                'page_size': 5,
                'keyword': 'k',
                'exists_only': False,
            })
        passed_filters = mocked.call_args.args[0]
        self.assertEqual(passed_filters['live_room_id'], 7)
        self.assertEqual(passed_filters['keyword'], 'k')
        self.assertEqual(mocked.call_args.kwargs, {'page': 2, 'page_size': 5})

    def test_exists_only_paginates_after_filtering_missing_records(self):
        import luboman.web as web
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, 'missing.flv')
            first = os.path.join(tmp, 'first.flv')
            second = os.path.join(tmp, 'second.flv')
            for video in (first, second):
                with open(video, 'wb') as fh:
                    fh.write(b'x')
            records = [
                self._record(missing, id=1),
                self._record(first, id=2),
                self._record(second, id=3),
            ]
            with self._patch_list(web, records):
                entries, total, _ = web._list_record_files_data({'page': 2, 'page_size': 1})

            self.assertEqual(total, 2)
            self.assertEqual([entry['id'] for entry in entries], [3])

    def test_room_summary_counts_existing_files_by_default(self):
        import luboman.web as web
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, 'missing.flv')
            existing = os.path.join(tmp, 'existing.flv')
            with open(existing, 'wb') as fh:
                fh.write(b'x')
            records = [
                self._record(missing, id=1, live_room_id=1, begin_time=datetime.datetime(2026, 1, 1)),
                self._record(existing, id=2, live_room_id=1, begin_time=datetime.datetime(2026, 1, 2)),
            ]
            summary = [{
                'live_room_id': 1,
                'room_name': 'streamerA',
                'room_platform': 'douyin',
                'room_owner': None,
                'room_url': None,
                'live_state': 0,
                'file_count': 2,
                'last_begin_time': datetime.datetime(2026, 1, 2),
            }]
            with patch.object(web.DB, 'list_record_file_room_summary', return_value=summary), \
                    self._patch_list(web, records):
                result = web._list_record_file_room_summary_data({})

            self.assertEqual(result[0]['file_count'], 1)
            self.assertEqual(result[0]['last_begin_time'], datetime.datetime(2026, 1, 2))

    def test_room_summary_can_include_missing_files(self):
        import luboman.web as web
        summary = [{'live_room_id': 1, 'file_count': 2, 'last_begin_time': None}]
        with patch.object(web.DB, 'list_record_file_room_summary', return_value=summary) as summary_mock, \
                patch.object(web.DB, 'list_record_file') as list_mock:
            result = web._list_record_file_room_summary_data({'exists_only': False})

        self.assertEqual(result, summary)
        summary_mock.assert_called_once()
        list_mock.assert_not_called()


class RecordFilePublishValidationTest(unittest.TestCase):
    """手动发布文件路径校验。"""

    def setUp(self):
        import luboman.web as web

        self.web = web
        self.tmp = tempfile.TemporaryDirectory()
        self.video_dir = os.path.realpath(os.path.join(self.tmp.name, 'video'))
        os.makedirs(self.video_dir)
        self.good = os.path.join(self.video_dir, 'a.flv')
        with open(self.good, 'wb') as fh:
            fh.write(b'x' * (6 * 1024 * 1024))
        self.threshold = 5 * 1024 * 1024

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_rejected(self):
        with self.assertRaises(ValueError):
            self.web._validate_publish_video_path(
                os.path.join(self.video_dir, 'nope.flv'), self.video_dir, self.threshold)

    def test_directory_traversal_rejected(self):
        outside = os.path.join(self.tmp.name, 'outside.flv')
        with open(outside, 'wb') as fh:
            fh.write(b'x' * (6 * 1024 * 1024))
        traversal = os.path.join(self.video_dir, '..', 'outside.flv')
        with self.assertRaises(ValueError):
            self.web._validate_publish_video_path(traversal, self.video_dir, self.threshold)

    def test_non_video_dir_rejected(self):
        with self.assertRaises(ValueError):
            self.web._validate_publish_video_path('/tmp/elsewhere.flv', self.video_dir, self.threshold)

    def test_below_threshold_rejected(self):
        small = os.path.join(self.video_dir, 'small.flv')
        with open(small, 'wb') as fh:
            fh.write(b'x')
        with self.assertRaises(ValueError):
            self.web._validate_publish_video_path(small, self.video_dir, self.threshold)

    def test_valid_file_normalized(self):
        real = self.web._validate_publish_video_path(self.good, self.video_dir, self.threshold)
        self.assertEqual(real, os.path.realpath(self.good))


class RecordFilePublishBiliTest(unittest.IsolatedAsyncioTestCase):
    """手动发布入队：mock 调度器与上传插件解析，验证上下文、优先级与返回值。"""

    def setUp(self):
        from luboman.database.models import LiveRoom, BiliAccount, BiliUploadTemplate

        self._restore_db = _swap_database_to_sqlite()
        self.account = BiliAccount.create(account_name='acc', bili_cookies='k=v;', state_active=1)
        self.template = BiliUploadTemplate.create(
            template_name='tpl', bili_account_id=self.account.id,
            tags=['x'], title='{room_name}')
        self.room = LiveRoom.create(
            room_url='http://t/1', room_name='streamerA',
            room_platform='douyin', room_title='title')

        self.tmp = tempfile.TemporaryDirectory()
        self.video_dir = os.path.realpath(os.path.join(self.tmp.name, 'video'))
        os.makedirs(self.video_dir)
        self.video = os.path.join(self.video_dir, 'a.flv')
        with open(self.video, 'wb') as fh:
            fh.write(b'x' * (6 * 1024 * 1024))

    def tearDown(self):
        self.tmp.cleanup()
        self._restore_db()

    async def test_publish_enqueues_with_high_priority_and_template_context(self):
        import luboman.web as web

        captured = {}

        async def fake_schedule(platform, file_list, room_data=None, priority=None):
            captured['platform'] = platform
            captured['file_list'] = file_list
            captured['room_data'] = room_data
            captured['priority'] = priority
            return 'task-123'

        request = _FakeRequest({
            'videos': [self.video],
            'bili_upload_template_id': self.template.id,
            'live_room_id': self.room.id,
        })

        orig_running = web.async_upload_scheduler.running
        orig_schedule = web.async_upload_scheduler.schedule_upload_simple
        orig_resolve = web.resolve_bili_uploader
        web.async_upload_scheduler.running = True
        web.async_upload_scheduler.schedule_upload_simple = fake_schedule
        web.resolve_bili_uploader = lambda room_data: 'biliup-rs'
        try:
            with patch.object(web, 'get_video_dir', return_value=self.video_dir):
                response = await web.publish_record_file_to_bili(request)
        finally:
            web.async_upload_scheduler.running = orig_running
            web.async_upload_scheduler.schedule_upload_simple = orig_schedule
            web.resolve_bili_uploader = orig_resolve

        body = json.loads(response.text)
        self.assertTrue(body['success'], body)
        self.assertEqual(body['data'], {
            'task_id': 'task-123', 'file_count': 1, 'uploader': 'biliup-rs',
        })
        self.assertEqual(captured['platform'], 'biliup-rs')
        self.assertEqual(captured['file_list'], [{'video': os.path.realpath(self.video)}])
        self.assertEqual(captured['priority'], web.UploadPriority.HIGH)

        template_info = captured['room_data']['bili_upload_template']
        self.assertEqual(template_info['id'], self.template.id)
        self.assertEqual(template_info['bili_account']['id'], self.account.id)
        self.assertEqual(captured['room_data']['room_name'], 'streamerA')

    async def test_publish_returns_error_when_scheduler_not_running(self):
        import luboman.web as web

        request = _FakeRequest({
            'videos': [self.video],
            'bili_upload_template_id': self.template.id,
            'live_room_id': self.room.id,
        })

        orig_running = web.async_upload_scheduler.running
        web.async_upload_scheduler.running = False
        try:
            with patch.object(web, 'get_video_dir', return_value=self.video_dir):
                response = await web.publish_record_file_to_bili(request)
        finally:
            web.async_upload_scheduler.running = orig_running

        body = json.loads(response.text)
        self.assertFalse(body['success'])
        self.assertIn('not running', body['message'])


class BiliUploadClipsOnlyTest(unittest.IsolatedAsyncioTestCase):
    """只投稿切片开关：读取点、整录投稿门闩、切片自动投稿仍走原链路。"""

    def test_should_auto_upload_full_bili(self):
        from luboman.database.db import should_auto_upload_full_bili

        self.assertTrue(should_auto_upload_full_bili(None))
        self.assertTrue(should_auto_upload_full_bili({}))
        self.assertTrue(should_auto_upload_full_bili({'bili_upload_clips_only': 0}))
        self.assertTrue(should_auto_upload_full_bili({'bili_upload_clips_only': None}))
        self.assertTrue(should_auto_upload_full_bili({'bili_upload_clips_only': 'nope'}))
        self.assertFalse(should_auto_upload_full_bili({'bili_upload_clips_only': 1}))
        self.assertFalse(should_auto_upload_full_bili({'bili_upload_clips_only': '1'}))

    def test_update_live_room_persists_clips_only_and_drops_junk(self):
        from peewee import SqliteDatabase
        import luboman.database.db as db_module
        from luboman.database.models import LiveRoom

        original_db = db_module.db
        tmp = tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False)
        tmp.close()
        test_db = SqliteDatabase(tmp.name)
        bind_ctx = test_db.bind_ctx([LiveRoom])
        bind_ctx.__enter__()
        db_module.db = test_db
        test_db.create_tables([LiveRoom])
        try:
            room = DB.create_live_room({
                'room_url': 'https://example.test/clips-only',
                'room_name': 'Clips',
                'ignored': 'drop-me',
            })
            self.assertEqual(room.get('bili_upload_clips_only', 0), 0)
            self.assertNotIn('ignored', room)
            self.assertEqual(DB.update_live_room({
                'id': room['id'],
                'bili_upload_clips_only': 1,
                'ignored': 'drop-me-too',
            }), 1)
            updated = DB.get_live_room_data(room['id'])
            self.assertEqual(updated['bili_upload_clips_only'], 1)
            self.assertNotIn('ignored', updated)
        finally:
            test_db.drop_tables([LiveRoom])
            db_module.db = original_db
            bind_ctx.__exit__(None, None, None)
            test_db.close()
            os.unlink(tmp.name)

    def _make_async_live(self, room_data):
        from luboman.core.async_live import AsyncLiveBase

        plugin = types.SimpleNamespace(
            room_name='r',
            room_url='http://example.test/live',
            room_data=room_data,
            log_prefix='[test]',
            raw_stream_url=None,
            is_living=False,
            living_time=0,
            is_recording=True,
            _active=True,
            suffix='flv',
            fake_headers={},
            event_manager=None,
        )
        return AsyncLiveBase(plugin)

    def _record_completed_handler(self, live):
        from luboman.core.async_event import AsyncEventType

        for event_type, handler in live._registered_handlers:
            if event_type == AsyncEventType.EVENT_RECORD_COMPLETED:
                return handler
        raise AssertionError('EVENT_RECORD_COMPLETED handler not registered')

    async def _emit_record_completed(self, room_data):
        from luboman.core.async_event import AsyncEvent, AsyncEventType

        live = self._make_async_live(room_data)
        sent = []

        async def capture(event):
            sent.append(event.type_)

        live.async_send_event = capture
        try:
            await self._record_completed_handler(live)(AsyncEvent(
                AsyncEventType.EVENT_RECORD_COMPLETED,
                args=([{'id': 1, 'video': '/tmp/a.flv'}],),
            ))
        finally:
            live._unregister_async_event_handlers()
        return sent

    async def test_record_completed_skips_full_bili_when_clips_only(self):
        from luboman.core.async_event import AsyncEventType

        sent = await self._emit_record_completed({
            'id': 7,
            'room_name': 'r',
            'bili_upload_template_ids': [9],
            'bili_upload_clips_only': 1,
            'auto_dance_clip': 0,
        })
        self.assertNotIn(AsyncEventType.EVENT_UPLOAD_BILI, sent)

    async def test_record_completed_uploads_full_bili_by_default(self):
        from luboman.core.async_event import AsyncEventType

        sent = await self._emit_record_completed({
            'id': 7,
            'room_name': 'r',
            'bili_upload_template_ids': [9],
            'bili_upload_clips_only': 0,
            'auto_dance_clip': 0,
        })
        self.assertIn(AsyncEventType.EVENT_UPLOAD_BILI, sent)

    async def test_auto_submit_clip_records_still_runs_when_clips_only(self):
        from luboman.core.dance_clip import auto_submit_clip_records

        room = {
            'id': 7,
            'room_name': 'r',
            'auto_dance_clip': 1,
            'bili_upload_clips_only': 1,
            'bili_upload_template_ids': [9],
        }
        record = {'id': 11, 'video': '/tmp/clip.mp4', 'begin_time': None}
        template = {'id': 9, 'template_name': 'tpl', 'bili_account_id': 1}
        scheduled = []

        async def fake_run_blocking(fn, *a, **k):
            return fn(*a, **k)

        async def fake_schedule(**kwargs):
            scheduled.append(kwargs)
            return {'task_id': 'clip-1'}

        with patch('luboman.core.dance_clip.is_daily_bili_merge_enabled', return_value=False), \
             patch('luboman.core.dance_clip.run_blocking', side_effect=fake_run_blocking), \
             patch('luboman.database.db.DB.get_live_room_data', return_value=room), \
             patch('luboman.database.db.DB.get_record_file', return_value=record), \
             patch('luboman.database.db.DB.get_bili_templates_with_accounts', return_value=[template]), \
             patch('luboman.database.db.DB.get_douyin_templates_with_accounts', return_value=[]), \
             patch('luboman.core.async_upload.schedule_bili_submission', side_effect=fake_schedule), \
             patch('luboman.core.dance_clip.os.path.isfile', return_value=True):
            await auto_submit_clip_records([11], 7)

        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]['file_list'], [{'id': 11, 'video': '/tmp/clip.mp4'}])
        self.assertEqual(scheduled[0]['metadata'], {'created_from': 'auto_dance_clip'})

    async def test_auto_submit_skips_instant_bili_when_daily_merge_on(self):
        from luboman.core.dance_clip import auto_submit_clip_records

        room = {
            'id': 7,
            'room_name': 'r',
            'auto_dance_clip': 1,
            'bili_upload_template_ids': [9],
        }
        record = {'id': 11, 'video': '/tmp/clip.mp4', 'begin_time': None}
        template = {'id': 9, 'template_name': 'tpl', 'bili_account_id': 1}
        scheduled = []

        async def fake_run_blocking(fn, *a, **k):
            return fn(*a, **k)

        async def fake_schedule(**kwargs):
            scheduled.append(kwargs)
            return {'task_id': 'clip-1'}

        with patch('luboman.core.dance_clip.is_daily_bili_merge_enabled', return_value=True), \
             patch('luboman.core.dance_clip.run_blocking', side_effect=fake_run_blocking), \
             patch('luboman.database.db.DB.get_live_room_data', return_value=room), \
             patch('luboman.database.db.DB.get_record_file', return_value=record), \
             patch('luboman.database.db.DB.get_bili_templates_with_accounts', return_value=[template]), \
             patch('luboman.database.db.DB.get_douyin_templates_with_accounts', return_value=[]), \
             patch('luboman.core.async_upload.schedule_bili_submission', side_effect=fake_schedule), \
             patch('luboman.core.dance_clip.os.path.isfile', return_value=True):
            await auto_submit_clip_records([11], 7)

        self.assertEqual(scheduled, [])


class DailyBiliClipMergeTest(unittest.IsolatedAsyncioTestCase):
    def test_clip_date_and_flush_decision(self):
        from luboman.core.dance_clip import _clip_date, _should_flush_date, _chunk_clips

        today = datetime.date(2026, 8, 13)
        self.assertEqual(
            _clip_date({'begin_time': datetime.datetime(2026, 8, 12, 23, 50)}),
            datetime.date(2026, 8, 12),
        )
        self.assertEqual(_clip_date({'begin_time': '2026-08-13 01:02:03'}), today)
        self.assertTrue(_should_flush_date(datetime.date(2026, 8, 12), today, True))
        self.assertFalse(_should_flush_date(today, today, True))
        self.assertTrue(_should_flush_date(today, today, False))
        self.assertEqual(len(_chunk_clips([{'id': i} for i in range(101)])), 2)
        self.assertEqual(len(_chunk_clips([{'id': i} for i in range(101)])[0]), 100)

    def test_room_is_live_prefers_memory_instance(self):
        from luboman.core.async_live import async_live_room_manager
        from luboman.core.dance_clip import _room_is_live

        class _FakeLive:
            is_living = False

        room = {'id': 7, 'live_state': 1}
        orig = dict(async_live_room_manager.live_rooms)
        async_live_room_manager.live_rooms['7'] = _FakeLive()
        try:
            self.assertFalse(_room_is_live(room))
        finally:
            async_live_room_manager.live_rooms.clear()
            async_live_room_manager.live_rooms.update(orig)
        self.assertTrue(_room_is_live(room))

    async def _flush(self, clips, room, today=None):
        from luboman.core.dance_clip import flush_daily_bili_clip_batches

        scheduled = []

        async def fake_run_blocking(fn, *a, **k):
            return fn(*a, **k)

        async def fake_schedule(**kwargs):
            scheduled.append(kwargs)
            return {'task_id': f't-{len(scheduled)}'}

        template = {'id': 9, 'template_name': 'tpl'}
        with patch('luboman.core.dance_clip.is_daily_bili_merge_enabled', return_value=True), \
             patch('luboman.core.dance_clip._today', return_value=today or datetime.date(2026, 8, 13)), \
             patch('luboman.core.dance_clip._ensure_part_title_path', side_effect=lambda src, title, cid: src), \
             patch('luboman.core.dance_clip.run_blocking', side_effect=fake_run_blocking), \
             patch('luboman.database.db.DB.list_pending_daily_bili_clips', return_value=clips), \
             patch('luboman.database.db.DB.get_live_room_data', return_value=room), \
             patch('luboman.database.db.DB.get_bili_templates_with_accounts', return_value=[template]), \
             patch('luboman.database.db.resolve_room_bili_template_ids', return_value=[9]), \
             patch('luboman.core.async_upload.schedule_bili_submission', side_effect=fake_schedule):
            results = await flush_daily_bili_clip_batches(7)
        return results, scheduled

    def _room(self, **overrides):
        room = {
            'id': 7,
            'room_name': '主播A',
            'auto_dance_clip': 1,
            'live_state': 0,
            'bili_upload_template_ids': [9],
        }
        room.update(overrides)
        return room

    def _clip(self, rid, when):
        return {
            'id': rid,
            'live_room_id': 7,
            'video': f'/tmp/c{rid}.mp4',
            'begin_time': when,
        }

    async def test_offline_same_day_packs_one_submission(self):
        clips = [
            self._clip(1, datetime.datetime(2026, 8, 13, 20, 0)),
            self._clip(2, datetime.datetime(2026, 8, 13, 21, 0)),
        ]
        results, scheduled = await self._flush(clips, self._room(live_state=0))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual([item['id'] for item in scheduled[0]['file_list']], [1, 2])
        self.assertEqual(scheduled[0]['metadata']['created_from'], 'auto_dance_clip_daily')
        self.assertEqual(scheduled[0]['metadata']['daily_date'], '2026-08-13')
        self.assertIn('舞蹈切片', scheduled[0]['room_data']['room_title'])

    async def test_live_same_day_waits(self):
        clips = [self._clip(1, datetime.datetime(2026, 8, 13, 20, 0))]
        results, scheduled = await self._flush(clips, self._room(live_state=1))
        self.assertEqual(results, [])
        self.assertEqual(scheduled, [])

    async def test_live_flushes_yesterday(self):
        clips = [
            self._clip(1, datetime.datetime(2026, 8, 12, 23, 0)),
            self._clip(2, datetime.datetime(2026, 8, 13, 1, 0)),
        ]
        results, scheduled = await self._flush(clips, self._room(live_state=1))
        self.assertEqual(len(scheduled), 1)
        self.assertEqual([item['id'] for item in scheduled[0]['file_list']], [1])
        self.assertEqual(scheduled[0]['metadata']['daily_date'], '2026-08-12')
        self.assertEqual(len(results), 1)

    async def test_splits_over_100_parts(self):
        clips = [
            self._clip(i, datetime.datetime(2026, 8, 13, 10, 0) + datetime.timedelta(minutes=i))
            for i in range(1, 102)
        ]
        _, scheduled = await self._flush(clips, self._room(live_state=0))
        self.assertEqual(len(scheduled), 2)
        self.assertEqual(len(scheduled[0]['file_list']), 100)
        self.assertEqual(len(scheduled[1]['file_list']), 1)
        self.assertIn('(2)', scheduled[1]['room_data']['room_title'])

    async def test_disabled_merge_does_not_flush(self):
        from luboman.core.dance_clip import flush_daily_bili_clip_batches

        with patch('luboman.core.dance_clip.is_daily_bili_merge_enabled', return_value=False):
            self.assertEqual(await flush_daily_bili_clip_batches(7), [])


if __name__ == "__main__":
    unittest.main()
