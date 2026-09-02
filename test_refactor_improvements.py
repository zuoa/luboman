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
            success, uploaded_files, failed_files, error, raw = await scheduler._perform_upload(task)
        finally:
            upload_module.upload = original_upload

        self.assertTrue(success)
        self.assertEqual(calls[0][0], "biliup-rs")
        self.assertEqual(calls[0][2], {"room_data": {"id": 1, "room_title": "title"}})
        self.assertEqual(uploaded_files, ["/tmp/not-exist.flv"])
        self.assertEqual(failed_files, [])
        self.assertIsNone(error)
        self.assertTrue(raw)

    async def test_perform_upload_resets_timestamps_when_flagged(self):
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
                file_list=[{"video": "/tmp/a.flv", "id": 8}],
                room_data={"id": 1},
                metadata={"reset_timestamps": True},
            )
            with patch(
                'luboman.core.upload_prep.reset_timestamps_in_file_list',
                return_value=[{"video": "/tmp/reset/a__reset.mp4", "id": 8}],
            ) as reset_mock:
                success, uploaded_files, failed_files, error, raw = await scheduler._perform_upload(task)
        finally:
            upload_module.upload = original_upload

        self.assertTrue(success)
        reset_mock.assert_called_once_with([{"video": "/tmp/a.flv", "id": 8}])
        self.assertEqual(uploaded_files, ["/tmp/reset/a__reset.mp4"])
        self.assertEqual(task.file_list, [{"video": "/tmp/reset/a__reset.mp4", "id": 8}])
        self.assertEqual(calls[0][1], [{"video": "/tmp/reset/a__reset.mp4", "id": 8}])
        self.assertIsNone(error)

    async def test_perform_upload_skips_reset_without_flag(self):
        import luboman.core.upload as upload_module

        scheduler = AsyncUploadScheduler(max_concurrent_uploads=1)
        original_upload = upload_module.upload
        upload_module.upload = lambda *a, **k: True
        try:
            task = AsyncUploadTask(
                platform="biliup-rs",
                file_list=[{"video": "/tmp/a.flv"}],
            )
            with patch('luboman.core.upload_prep.reset_timestamps_in_file_list') as reset_mock:
                await scheduler._perform_upload(task)
        finally:
            upload_module.upload = original_upload

        reset_mock.assert_not_called()

    async def test_perform_upload_fails_when_reset_raises(self):
        import luboman.core.upload as upload_module

        scheduler = AsyncUploadScheduler(max_concurrent_uploads=1)
        calls = []
        original_upload = upload_module.upload
        upload_module.upload = lambda *a, **k: calls.append(a) or True
        try:
            task = AsyncUploadTask(
                platform="biliup-rs",
                file_list=[{"video": "/tmp/a.flv"}],
                metadata={"reset_timestamps": True},
            )
            with patch(
                'luboman.core.upload_prep.reset_timestamps_in_file_list',
                side_effect=RuntimeError('ffmpeg timestamp reset failed'),
            ):
                success, uploaded_files, failed_files, error, raw = await scheduler._perform_upload(task)
        finally:
            upload_module.upload = original_upload

        self.assertFalse(success)
        self.assertEqual(calls, [])
        self.assertIn('ffmpeg timestamp reset failed', error or '')
        self.assertEqual(failed_files, ["/tmp/a.flv"])

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

    async def test_bili_account_upower_levels_route_uses_db_executor(self):
        original_run_db = web_module.run_db
        calls = []

        async def fake_run_db(func, *args, **kwargs):
            calls.append((func.__name__, args))
            return {'levels': [], 'selected_id': None}

        web_module.run_db = fake_run_db
        try:
            missing = await web_module.list_bili_account_upower_levels(self._Request({}))
            response = await web_module.list_bili_account_upower_levels(self._Request({"id": 9}))
        finally:
            web_module.run_db = original_run_db

        self.assertFalse(self._response_json(missing)["success"])
        data = self._response_json(response)
        self.assertTrue(data["success"])
        self.assertEqual(calls, [("_list_bili_account_upower_levels", (9,))])


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
        self.assertIn('@routes.post("/v1/BiliAccount/upowerLevels")', source)
        self.assertIn("await run_db(_list_bili_account_upower_levels, account_id)", source)

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


class RecordFilePublishStorageTest(unittest.IsolatedAsyncioTestCase):
    """手动存网盘：平台校验、路径组装、入队。"""

    def setUp(self):
        import luboman.web as web

        self.web = web
        self.tmp = tempfile.TemporaryDirectory()
        self.video_dir = os.path.realpath(os.path.join(self.tmp.name, 'video'))
        os.makedirs(self.video_dir)
        self.video = os.path.join(self.video_dir, 'a.flv')
        with open(self.video, 'wb') as fh:
            fh.write(b'x' * (6 * 1024 * 1024))

    def tearDown(self):
        self.tmp.cleanup()

    def test_prepare_requires_platform_or_room(self):
        with self.assertRaises(ValueError) as ctx:
            self.web._prepare_storage_publish({'videos': [self.video]})
        self.assertIn('upload_storage_platform', str(ctx.exception))

    def test_prepare_rejects_unknown_platform(self):
        with self.assertRaises(ValueError):
            self.web._prepare_storage_publish({
                'videos': [self.video],
                'upload_storage_platform': 'dropbox',
            })

    def test_prepare_assembles_file_list(self):
        with patch.object(self.web, 'get_video_dir', return_value=self.video_dir), \
             patch.object(self.web.config, 'get', return_value=5):
            platform, live_room_id, file_list = self.web._prepare_storage_publish({
                'videos': [self.video],
                'upload_storage_platform': 'quark',
                'live_room_id': 9,
            })
        self.assertEqual(platform, 'quark')
        self.assertEqual(live_room_id, 9)
        self.assertEqual(file_list, [{'video': os.path.realpath(self.video)}])

    async def test_publish_storage_enqueues_task(self):
        import luboman.web as web

        scheduled = {}

        async def fake_schedule(**kwargs):
            scheduled.update(kwargs)
            return {'task_id': 'storage-1', 'file_count': 1, 'uploader': 'quark'}

        async def fake_run_db(fn, *a, **k):
            return fn(*a, **k)

        request = _FakeRequest({
            'videos': [self.video],
            'upload_storage_platform': 'quark',
        })

        with patch.object(web, 'get_video_dir', return_value=self.video_dir), \
             patch.object(web.config, 'get', return_value=5), \
             patch.object(web, 'run_db', side_effect=fake_run_db), \
             patch.object(web, 'schedule_storage_upload', side_effect=fake_schedule):
            response = await web.publish_record_file_to_storage(request)

        body = json.loads(response.text)
        self.assertTrue(body['success'], body)
        self.assertEqual(body['data']['tasks'][0]['task_id'], 'storage-1')
        self.assertEqual(scheduled['platform'], 'quark')
        self.assertEqual(scheduled['source'], web.SUBMISSION_TASK_SOURCE_FILE_MANAGER)
        self.assertEqual(scheduled['file_list'], [{'video': os.path.realpath(self.video)}])


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


class RecordFilePublishResetTimestampsTest(unittest.IsolatedAsyncioTestCase):
    """手动投稿 reset_timestamps 标志会传给 B 站调度器。"""

    async def test_publish_passes_reset_timestamps_to_scheduler(self):
        import luboman.web as web

        captured = {}
        file_list = [{'video': '/tmp/a.flv'}]
        room_data = {'bili_upload_template': {'id': 1}}

        async def fake_schedule(**kwargs):
            captured.update(kwargs)
            return {'task_id': 'task-reset', 'file_count': 1, 'uploader': 'biliup-rs'}

        async def fake_run_db(fn, *a, **k):
            if fn is web._prepare_bili_publish:
                return [1], 9, {}, file_list
            if fn is web._build_bili_publish_room_data:
                return room_data
            return fn(*a, **k)

        request = _FakeRequest({
            'videos': ['/tmp/a.flv'],
            'bili_upload_template_ids': [1],
            'reset_timestamps': True,
        })

        with patch.object(web, 'run_db', side_effect=fake_run_db), \
             patch.object(web, 'schedule_bili_submission', side_effect=fake_schedule):
            response = await web.publish_record_file_to_bili(request)

        body = json.loads(response.text)
        self.assertTrue(body['success'], body)
        self.assertTrue(captured['reset_timestamps'])
        self.assertEqual(captured['source'], web.SUBMISSION_TASK_SOURCE_FILE_MANAGER)
        self.assertEqual(captured['file_list'], file_list)
        self.assertEqual(captured['room_data'], room_data)

    async def test_publish_defaults_reset_timestamps_off(self):
        import luboman.web as web

        captured = {}

        async def fake_schedule(**kwargs):
            captured.update(kwargs)
            return {'task_id': 'task-plain', 'file_count': 1, 'uploader': 'biliup-rs'}

        async def fake_run_db(fn, *a, **k):
            if fn is web._prepare_bili_publish:
                return [1], None, {}, [{'video': '/tmp/a.flv'}]
            if fn is web._build_bili_publish_room_data:
                return {}
            return fn(*a, **k)

        request = _FakeRequest({
            'videos': ['/tmp/a.flv'],
            'bili_upload_template_ids': [1],
        })

        with patch.object(web, 'run_db', side_effect=fake_run_db), \
             patch.object(web, 'schedule_bili_submission', side_effect=fake_schedule):
            response = await web.publish_record_file_to_bili(request)

        body = json.loads(response.text)
        self.assertTrue(body['success'], body)
        self.assertFalse(captured['reset_timestamps'])

    async def test_publish_applies_upower_override(self):
        import luboman.web as web

        captured = {}
        room_data = {
            'bili_upower_enabled': 1,
            'bili_upower_level_id': 'old-room-level',
            'bili_upload_template': {
                'id': 1,
                'bili_account': {'upower_level_id': '952390697301177415'},
            },
        }

        async def fake_schedule(**kwargs):
            captured.update(kwargs)
            return {'task_id': 'task-upower', 'file_count': 1, 'uploader': 'biliup-rs'}

        async def fake_run_db(fn, *a, **k):
            if fn is web._prepare_bili_publish:
                return [1], 9, {}, [{'video': '/tmp/a.flv'}]
            if fn is web._build_bili_publish_room_data:
                return dict(room_data)
            return fn(*a, **k)

        request = _FakeRequest({
            'videos': ['/tmp/a.flv'],
            'bili_upload_template_ids': [1],
            'bili_upower_enabled': False,
        })

        with patch.object(web, 'run_db', side_effect=fake_run_db), \
             patch.object(web, 'schedule_bili_submission', side_effect=fake_schedule):
            response = await web.publish_record_file_to_bili(request)

        body = json.loads(response.text)
        self.assertTrue(body['success'], body)
        self.assertEqual(captured['room_data']['bili_upower_enabled'], 0)
        self.assertIsNone(captured['room_data']['bili_upower_level_id'])

        request_on = _FakeRequest({
            'videos': ['/tmp/a.flv'],
            'bili_upload_template_ids': [1],
            'bili_upower_enabled': True,
        })
        with patch.object(web, 'run_db', side_effect=fake_run_db), \
             patch.object(web, 'schedule_bili_submission', side_effect=fake_schedule):
            response_on = await web.publish_record_file_to_bili(request_on)
        body_on = json.loads(response_on.text)
        self.assertTrue(body_on['success'], body_on)
        self.assertEqual(captured['room_data']['bili_upower_enabled'], 1)
        self.assertEqual(captured['room_data']['bili_upower_level_id'], 'old-room-level')


class UploadPrepTimestampResetTest(unittest.TestCase):
    """投稿前时间戳重置：缓存复用、ffmpeg 命令、失败不降级原文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, 'live.flv')
        with open(self.src, 'wb') as fh:
            fh.write(b'x' * 1024)
        self.src_info = {
            'duration': 10.0,
            'video': {'codec_name': 'h264'},
            'audio': {'codec_name': 'aac'},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _dst(self):
        return os.path.join(self.tmp.name, 'reset', 'live__reset.mp4')

    def test_reuses_cached_reset_file(self):
        from luboman.core import upload_prep

        dst = self._dst()
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'wb') as fh:
            fh.write(b'cached')
        os.utime(dst, (os.path.getmtime(self.src) + 10, os.path.getmtime(self.src) + 10))

        with patch.object(upload_prep, '_reset_timestamps_ffmpeg') as ffmpeg:
            result = upload_prep.ensure_timestamps_reset(self.src)
        self.assertEqual(result, dst)
        ffmpeg.assert_not_called()

    def test_audio_reencode_command_and_success(self):
        from luboman.core import upload_prep

        dst = self._dst()

        def fake_ffmpeg(src, out, src_info, reencode_video=False):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as fh:
                fh.write(b'reset')
            self.assertFalse(reencode_video)

        with patch.object(upload_prep, '_probe_streams', side_effect=[
            self.src_info,
            {'duration': 10.1},
        ]), patch.object(upload_prep, '_reset_timestamps_ffmpeg', side_effect=fake_ffmpeg):
            result = upload_prep.ensure_timestamps_reset(self.src)
        self.assertEqual(result, dst)
        self.assertTrue(os.path.isfile(dst))

    def test_falls_back_to_full_reencode_when_duration_mismatch(self):
        from luboman.core import upload_prep

        calls = []

        def fake_ffmpeg(src, out, src_info, reencode_video=False):
            calls.append(reencode_video)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as fh:
                fh.write(b'reset')

        with patch.object(upload_prep, '_probe_streams', side_effect=[
            self.src_info,
            {'duration': 1.0},
            {'duration': 10.0},
        ]), patch.object(upload_prep, '_reset_timestamps_ffmpeg', side_effect=fake_ffmpeg):
            result = upload_prep.ensure_timestamps_reset(self.src)
        self.assertEqual(result, self._dst())
        self.assertEqual(calls, [False, True])

    def test_skips_already_reset_file(self):
        from luboman.core import upload_prep

        reset_dir = os.path.join(self.tmp.name, 'reset')
        os.makedirs(reset_dir, exist_ok=True)
        already = os.path.join(reset_dir, 'live__reset.mp4')
        with open(already, 'wb') as fh:
            fh.write(b'done')
        with patch.object(upload_prep, '_reset_timestamps_ffmpeg') as ffmpeg:
            self.assertEqual(upload_prep.ensure_timestamps_reset(already), already)
        ffmpeg.assert_not_called()

    def test_file_list_maps_videos_and_keeps_ids(self):
        from luboman.core import upload_prep

        with patch.object(upload_prep, 'ensure_timestamps_reset', return_value='/tmp/r.mp4') as ensure:
            result = upload_prep.reset_timestamps_in_file_list([
                {'video': self.src, 'id': 3},
            ])
        ensure.assert_called_once_with(self.src)
        self.assertEqual(result, [{'video': '/tmp/r.mp4', 'id': 3}])

    def test_file_list_raises_when_source_missing(self):
        from luboman.core import upload_prep

        with self.assertRaises(ValueError):
            upload_prep.reset_timestamps_in_file_list([{'video': '/no/such.flv'}])

    def test_ffmpeg_command_reencodes_audio(self):
        from luboman.core import upload_prep

        captured = {}

        def fake_run(command, timeout, desc):
            captured['command'] = command
            captured['desc'] = desc

        with patch.object(upload_prep, '_run_ffmpeg', side_effect=fake_run), \
             patch.object(upload_prep.config, 'get', return_value='ffmpeg'):
            upload_prep._reset_timestamps_ffmpeg(
                self.src, self._dst(), self.src_info, reencode_video=False,
            )
        command = captured['command']
        self.assertIn('-c:v', command)
        self.assertEqual(command[command.index('-c:v') + 1], 'copy')
        self.assertIn('-c:a', command)
        self.assertEqual(command[command.index('-c:a') + 1], 'aac')
        self.assertIn('aresample=async=1:first_pts=0', command)
        self.assertEqual(captured['desc'], '时间戳重置(音频重编码)')

    def test_failed_reset_deletes_partial_output(self):
        from luboman.core import upload_prep

        def fake_ffmpeg(src, out, src_info, reencode_video=False):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as fh:
                fh.write(b'bad')
            if reencode_video:
                raise RuntimeError('full reencode failed')

        with patch.object(upload_prep, '_probe_streams', return_value=self.src_info), \
             patch.object(upload_prep, '_reset_timestamps_ffmpeg', side_effect=fake_ffmpeg), \
             patch.object(upload_prep, '_duration_close', return_value=False):
            with self.assertRaises(RuntimeError):
                upload_prep.ensure_timestamps_reset(self.src)
        self.assertFalse(os.path.isfile(self._dst()))


class ScheduleBiliResetTimestampsTest(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_bili_submission_writes_reset_flag_to_metadata(self):
        from luboman.core import async_upload

        captured = {}

        async def fake_schedule_submission(**kwargs):
            captured.update(kwargs)
            return {'task_id': 't1'}

        with patch.object(async_upload, 'schedule_submission', side_effect=fake_schedule_submission), \
             patch('luboman.core.upload.resolve_bili_uploader', return_value='biliup-rs'):
            await async_upload.schedule_bili_submission(
                file_list=[{'video': '/tmp/a.flv'}],
                room_data={},
                reset_timestamps=True,
                metadata={'created_from': 'record_file'},
            )
        self.assertTrue(captured['metadata']['reset_timestamps'])
        self.assertEqual(captured['metadata']['created_from'], 'record_file')
        self.assertEqual(captured['file_list'], [{'video': '/tmp/a.flv'}])

    async def test_schedule_bili_submission_omits_reset_flag_by_default(self):
        from luboman.core import async_upload

        captured = {}

        async def fake_schedule_submission(**kwargs):
            captured.update(kwargs)
            return {'task_id': 't1'}

        with patch.object(async_upload, 'schedule_submission', side_effect=fake_schedule_submission), \
             patch('luboman.core.upload.resolve_bili_uploader', return_value='biliup-rs'):
            await async_upload.schedule_bili_submission(
                file_list=[{'video': '/tmp/a.flv'}],
                room_data={},
            )
        self.assertNotIn('reset_timestamps', captured['metadata'] or {})


class BiliUploadClipsOnlyTest(unittest.IsolatedAsyncioTestCase):
    """只投稿切片开关：读取点、整录投稿门闩（B站+网盘）、切片自动投稿仍走原链路。"""

    def test_should_auto_upload_full(self):
        from luboman.database.db import (
            is_upload_clips_only,
            should_auto_upload_full,
            should_auto_upload_full_bili,
        )

        self.assertFalse(is_upload_clips_only(None))
        self.assertFalse(is_upload_clips_only({}))
        self.assertFalse(is_upload_clips_only({'bili_upload_clips_only': 0}))
        self.assertFalse(is_upload_clips_only({'bili_upload_clips_only': None}))
        self.assertFalse(is_upload_clips_only({'bili_upload_clips_only': 'nope'}))
        self.assertTrue(is_upload_clips_only({'bili_upload_clips_only': 1}))
        self.assertTrue(is_upload_clips_only({'bili_upload_clips_only': '1'}))

        self.assertTrue(should_auto_upload_full(None))
        self.assertTrue(should_auto_upload_full({}))
        self.assertTrue(should_auto_upload_full({'bili_upload_clips_only': 0}))
        self.assertTrue(should_auto_upload_full({'bili_upload_clips_only': None}))
        self.assertTrue(should_auto_upload_full({'bili_upload_clips_only': 'nope'}))
        self.assertFalse(should_auto_upload_full({'bili_upload_clips_only': 1}))
        self.assertFalse(should_auto_upload_full({'bili_upload_clips_only': '1'}))
        self.assertIs(should_auto_upload_full_bili, should_auto_upload_full)

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

    async def test_record_completed_skips_full_storage_when_clips_only(self):
        from luboman.core.async_event import AsyncEventType

        sent = await self._emit_record_completed({
            'id': 7,
            'room_name': 'r',
            'auto_upload': True,
            'upload_storage_platform': 'quark',
            'bili_upload_clips_only': 1,
            'auto_dance_clip': 0,
        })
        self.assertNotIn(AsyncEventType.EVENT_UPLOAD, sent)
        self.assertNotIn(AsyncEventType.EVENT_UPLOAD_BILI, sent)

    async def test_record_completed_uploads_full_storage_by_default(self):
        from luboman.core.async_event import AsyncEventType

        sent = await self._emit_record_completed({
            'id': 7,
            'room_name': 'r',
            'auto_upload': True,
            'upload_storage_platform': 'quark',
            'bili_upload_clips_only': 0,
            'auto_dance_clip': 0,
        })
        self.assertIn(AsyncEventType.EVENT_UPLOAD, sent)

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

    async def test_auto_submit_clip_records_uploads_storage_when_clips_only(self):
        from luboman.core.dance_clip import auto_submit_clip_records

        room = {
            'id': 7,
            'room_name': 'r',
            'auto_dance_clip': 1,
            'bili_upload_clips_only': 1,
            'upload_storage_platform': 'quark',
        }
        record = {'id': 11, 'video': '/tmp/clip.mp4', 'begin_time': None}
        scheduled = []

        async def fake_run_blocking(fn, *a, **k):
            return fn(*a, **k)

        async def fake_schedule(**kwargs):
            scheduled.append(kwargs)
            return 'storage-1'

        with patch('luboman.core.dance_clip.is_daily_bili_merge_enabled', return_value=False), \
             patch('luboman.core.dance_clip.run_blocking', side_effect=fake_run_blocking), \
             patch('luboman.database.db.DB.get_live_room_data', return_value=room), \
             patch('luboman.database.db.DB.get_record_file', return_value=record), \
             patch('luboman.database.db.DB.get_bili_templates_with_accounts', return_value=[]), \
             patch('luboman.database.db.DB.get_douyin_templates_with_accounts', return_value=[]), \
             patch(
                 'luboman.core.async_upload.async_upload_scheduler.schedule_upload_simple',
                 side_effect=fake_schedule,
             ), \
             patch('luboman.core.dance_clip.os.path.isfile', return_value=True):
            await auto_submit_clip_records([11], 7)

        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]['platform'], 'quark')
        self.assertEqual(scheduled[0]['file_list'], [{'id': 11, 'video': '/tmp/clip.mp4'}])
        self.assertEqual(scheduled[0]['metadata'], {'created_from': 'auto_dance_clip'})

    async def test_auto_submit_skips_storage_when_clips_only_off(self):
        from luboman.core.dance_clip import auto_submit_clip_records

        room = {
            'id': 7,
            'room_name': 'r',
            'auto_dance_clip': 1,
            'bili_upload_clips_only': 0,
            'upload_storage_platform': 'quark',
            'bili_upload_template_ids': [9],
        }
        record = {'id': 11, 'video': '/tmp/clip.mp4', 'begin_time': None}
        template = {'id': 9, 'template_name': 'tpl', 'bili_account_id': 1}
        storage_scheduled = []

        async def fake_run_blocking(fn, *a, **k):
            return fn(*a, **k)

        async def fake_bili(**kwargs):
            return {'task_id': 'clip-1'}

        async def fake_storage(**kwargs):
            storage_scheduled.append(kwargs)
            return 'storage-1'

        with patch('luboman.core.dance_clip.is_daily_bili_merge_enabled', return_value=False), \
             patch('luboman.core.dance_clip.run_blocking', side_effect=fake_run_blocking), \
             patch('luboman.database.db.DB.get_live_room_data', return_value=room), \
             patch('luboman.database.db.DB.get_record_file', return_value=record), \
             patch('luboman.database.db.DB.get_bili_templates_with_accounts', return_value=[template]), \
             patch('luboman.database.db.DB.get_douyin_templates_with_accounts', return_value=[]), \
             patch('luboman.core.async_upload.schedule_bili_submission', side_effect=fake_bili), \
             patch(
                 'luboman.core.async_upload.async_upload_scheduler.schedule_upload_simple',
                 side_effect=fake_storage,
             ), \
             patch('luboman.core.dance_clip.os.path.isfile', return_value=True):
            await auto_submit_clip_records([11], 7)

        self.assertEqual(storage_scheduled, [])

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


class BiliPublishWatchTest(unittest.TestCase):
    """BV 号提取、公开页判定、8 小时窗口轮询。"""

    def test_extract_bvid_from_nested_result(self):
        from luboman.core.bili_publish import extract_bvid

        self.assertEqual(
            extract_bvid({'data': {'bvid': 'BV1xx411c7mD'}}),
            'BV1xx411c7mD',
        )
        self.assertEqual(
            extract_bvid({'raw_result': {'output_tail': ['ok', 'bvid: BV1yy411c7AA']}}),
            'BV1yy411c7AA',
        )
        self.assertEqual(
            extract_bvid({'result': {'raw_result': {'data': {'bvid': 'BV1zz411c7BB'}}}}),
            'BV1zz411c7BB',
        )
        self.assertIsNone(extract_bvid({'success': True, 'output_tail': ['no id']}))

    def test_check_bvid_published_true_false_none(self):
        from luboman.core import bili_publish

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        with patch.object(bili_publish.requests, 'get', return_value=FakeResp({
            'code': 0, 'data': {'bvid': 'BV1xx411c7mD', 'aid': 1},
        })):
            self.assertTrue(bili_publish.check_bvid_published('BV1xx411c7mD'))

        with patch.object(bili_publish.requests, 'get', return_value=FakeResp({
            'code': -404, 'data': None,
        })):
            self.assertFalse(bili_publish.check_bvid_published('BV1xx411c7mD'))

        with patch.object(bili_publish.requests, 'get', side_effect=RuntimeError('timeout')):
            self.assertIsNone(bili_publish.check_bvid_published('BV1xx411c7mD'))

        self.assertIsNone(bili_publish.check_bvid_published('not-a-bvid'))

    def test_watch_marks_published_and_skips_missing_bvid(self):
        from luboman.core import bili_publish
        from luboman.database import db as db_module

        tasks = [
            {'task_id': 't-pub', 'bvid': 'BV1xx411c7mD', 'result': None},
            {'task_id': 't-wait', 'bvid': 'BV1yy411c7AA', 'result': None},
            {'task_id': 't-skip', 'bvid': None, 'result': {'ok': True}},
        ]
        published = []
        checked = []

        with patch.object(db_module.DB, 'list_bili_publish_watch_tasks', return_value=tasks), \
             patch.object(db_module.DB, 'mark_submission_task_published', side_effect=lambda *a, **k: published.append(k or a)), \
             patch.object(db_module.DB, 'mark_submission_task_publish_checked', side_effect=lambda *a, **k: checked.append(a)), \
             patch.object(bili_publish, 'check_bvid_published', side_effect=lambda bvid: bvid == 'BV1xx411c7mD'):
            stats = bili_publish.watch_pending_publications()

        self.assertEqual(stats['published'], 1)
        self.assertEqual(stats['checked'], 2)
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(published[0]['bvid'], 'BV1xx411c7mD')
        self.assertEqual(checked[0][0], 't-wait')

    def test_watch_backfills_bvid_from_result(self):
        from luboman.core import bili_publish
        from luboman.database import db as db_module

        saved = []
        with patch.object(db_module.DB, 'list_bili_publish_watch_tasks', return_value=[{
            'task_id': 't1',
            'bvid': None,
            'result': {'raw_result': {'output_tail': ['Submit BV1aa411c7CC']}},
        }]), patch.object(db_module.DB, 'save_submission_task_bvid', side_effect=lambda *a, **k: saved.append(a)), \
             patch.object(db_module.DB, 'mark_submission_task_published') as mark_pub, \
             patch.object(bili_publish, 'check_bvid_published', return_value=True):
            stats = bili_publish.watch_pending_publications()

        self.assertEqual(saved[0], ('t1', 'BV1aa411c7CC'))
        mark_pub.assert_called_once()
        self.assertEqual(stats['published'], 1)

    def test_finish_submission_task_records_bvid_as_reviewing(self):
        from peewee import SqliteDatabase
        from luboman.database import models as models_module
        from luboman.database import db as db_module
        from luboman.database.models import SubmissionTask, RecordFile, LiveRoom
        from luboman.database.db import (
            DB, SUBMISSION_TASK_STATUS_SUCCESS, PUBLISH_STATUS_REVIEWING,
        )

        db_path = tempfile.mktemp(suffix='.db')
        test_db = SqliteDatabase(db_path)
        original = (models_module.db, db_module.db)
        models = [LiveRoom, RecordFile, SubmissionTask]
        models_module.db = test_db
        db_module.db = test_db
        for model in models:
            model.bind(test_db)
        test_db.connect(reuse_if_open=True)
        test_db.create_tables(models)
        try:
            room = LiveRoom.create(room_url='http://t/1', room_name='A')
            rec = RecordFile.create(
                live_room_id=room.id, begin_time=datetime.datetime.now(),
                video='/tmp/a.flv',
            )
            SubmissionTask.create(
                task_id='t-bvid',
                platform='biliup-rs',
                status='RUNNING',
                file_list=[{'video': '/tmp/a.flv', 'id': rec.id}],
                record_file_ids=[rec.id],
            )
            DB.finish_submission_task(
                't-bvid', True,
                result={'raw_result': {'data': {'bvid': 'BV1xx411c7mD'}}},
            )
            task = SubmissionTask.get(SubmissionTask.task_id == 't-bvid')
            self.assertEqual(task.status, SUBMISSION_TASK_STATUS_SUCCESS)
            self.assertEqual(task.bvid, 'BV1xx411c7mD')
            self.assertEqual(task.publish_status, PUBLISH_STATUS_REVIEWING)
            rec = RecordFile.get_by_id(rec.id)
            self.assertEqual(rec.upload_info['bvid'], 'BV1xx411c7mD')
            self.assertEqual(rec.upload_info['publish_status'], PUBLISH_STATUS_REVIEWING)
        finally:
            test_db.close()
            for model in models:
                model.bind(original[0])
            models_module.db = original[0]
            db_module.db = original[1]
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_list_watch_tasks_excludes_published_and_expired(self):
        from peewee import SqliteDatabase
        from luboman.database import models as models_module
        from luboman.database import db as db_module
        from luboman.database.models import SubmissionTask
        from luboman.database.db import DB, PUBLISH_STATUS_REVIEWING, PUBLISH_STATUS_PUBLISHED

        db_path = tempfile.mktemp(suffix='.db')
        test_db = SqliteDatabase(db_path)
        original = (models_module.db, db_module.db)
        models_module.db = test_db
        db_module.db = test_db
        SubmissionTask.bind(test_db)
        test_db.connect(reuse_if_open=True)
        test_db.create_tables([SubmissionTask])
        try:
            now = datetime.datetime.now()
            SubmissionTask.create(
                task_id='fresh', platform='biliup-rs', status='SUCCESS',
                file_list=[], bvid='BV1aa411c7AA',
                publish_status=PUBLISH_STATUS_REVIEWING, finished_at=now,
            )
            SubmissionTask.create(
                task_id='done', platform='biliup-rs', status='SUCCESS',
                file_list=[], bvid='BV1bb411c7BB',
                publish_status=PUBLISH_STATUS_PUBLISHED, finished_at=now,
            )
            SubmissionTask.create(
                task_id='old', platform='biliup-rs', status='SUCCESS',
                file_list=[], bvid='BV1cc411c7CC',
                publish_status=PUBLISH_STATUS_REVIEWING,
                finished_at=now - datetime.timedelta(hours=9),
            )
            rows = DB.list_bili_publish_watch_tasks(now=now, hours=8)
            ids = [row['task_id'] for row in rows]
            self.assertEqual(ids, ['fresh'])
        finally:
            test_db.close()
            SubmissionTask.bind(original[0])
            models_module.db = original[0]
            db_module.db = original[1]
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_ensure_submission_task_publish_schema_before_create_table(self):
        """旧表缺 bvid 时，先补列再 create_table，避免 Peewee 对不存在的列建索引。"""
        from peewee import SqliteDatabase
        from luboman.database import models as models_module
        from luboman.database import db as db_module
        from luboman.database.models import SubmissionTask
        from luboman.database.db import DB

        db_path = tempfile.mktemp(suffix='.db')
        test_db = SqliteDatabase(db_path)
        original = (models_module.db, db_module.db)
        models_module.db = test_db
        db_module.db = test_db
        SubmissionTask.bind(test_db)
        test_db.connect(reuse_if_open=True)
        test_db.execute_sql('''
            CREATE TABLE submissiontask (
                id INTEGER PRIMARY KEY,
                task_id VARCHAR(255) NOT NULL,
                source VARCHAR(255),
                platform VARCHAR(255),
                status VARCHAR(255),
                priority VARCHAR(255),
                file_list TEXT,
                file_count INTEGER,
                record_file_ids TEXT,
                live_room_id INTEGER,
                room_name VARCHAR(255),
                room_platform VARCHAR(255),
                bili_upload_template_id INTEGER,
                bili_upload_template_name VARCHAR(255),
                uploader VARCHAR(255),
                retry_count INTEGER,
                max_retries INTEGER,
                error_message TEXT,
                result TEXT,
                metadata TEXT,
                created_at DATETIME,
                updated_at DATETIME,
                started_at DATETIME,
                finished_at DATETIME
            )
        ''')
        try:
            cols_before = {c.name for c in test_db.get_columns('submissiontask')}
            self.assertNotIn('bvid', cols_before)
            DB._ensure_submission_task_publish_schema()
            SubmissionTask.create_table(safe=True)
            cols = {c.name for c in test_db.get_columns('submissiontask')}
            self.assertIn('bvid', cols)
            self.assertIn('publish_status', cols)
            self.assertIn('publish_checked_at', cols)
        finally:
            test_db.close()
            SubmissionTask.bind(original[0])
            models_module.db = original[0]
            db_module.db = original[1]
            try:
                os.remove(db_path)
            except OSError:
                pass


class BiliUpowerDbTest(unittest.TestCase):
    def setUp(self):
        from peewee import SqliteDatabase
        import luboman.database.db as db_module
        from luboman.database.models import BiliAccount, LiveRoom

        self.db_module = db_module
        self.original_db = db_module.db
        self.models = [LiveRoom, BiliAccount]
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

    def test_account_upower_level_and_room_switch(self):
        account = DB.create_bili_account({
            "account_name": "Uploader",
            "bili_cookies": "SESSDATA=x;",
        })
        self.assertEqual(DB.update_bili_account({
            "id": account["id"],
            "upower_level_id": "952390697301177415",
            "ignored": "x",
        }), 1)
        self.assertEqual(DB.list_bili_account()[0]["upower_level_id"], "952390697301177415")
        self.assertEqual(DB.update_bili_account({"id": account["id"], "upower_level_id": ""}), 1)
        self.assertIsNone(DB.list_bili_account()[0]["upower_level_id"])

        room = DB.create_live_room({
            "room_url": "https://example.test/upower",
            "room_name": "Upower",
            "bili_upower_level_id": "old-room-level",
        })
        self.assertEqual(DB.update_live_room({
            "id": room["id"],
            "bili_upower_enabled": 1,
        }), 1)
        self.assertEqual(DB.get_live_room_data(room["id"])["bili_upower_enabled"], 1)
        self.assertEqual(DB.update_live_room({"id": room["id"], "bili_upower_enabled": 0}), 1)
        closed = DB.get_live_room_data(room["id"])
        self.assertEqual(closed["bili_upower_enabled"], 0)
        self.assertIsNone(closed["bili_upower_level_id"])


class BiliUpowerResolveTest(unittest.TestCase):
    def test_resolve_bili_upower_uses_account_level_when_room_enabled(self):
        from luboman.core.bili_upower import resolve_bili_upower

        self.assertIsNone(resolve_bili_upower({'bili_upower_enabled': 0}, {
            'bili_account': {'upower_level_id': '952390697301177415'},
        }))
        self.assertEqual(
            resolve_bili_upower({'bili_upower_enabled': 1}, {
                'bili_account': {'upower_level_id': '952390697301177415'},
            }),
            {'charging_pay': 1, 'upower_level_id': '952390697301177415'},
        )

    def test_resolve_bili_upower_legacy_room_level_still_enables(self):
        from luboman.core.bili_upower import resolve_bili_upower

        self.assertEqual(
            resolve_bili_upower({
                'bili_upower_enabled': 0,
                'bili_upower_level_id': 'old-room-level',
            }, {'bili_account': {}}),
            {'charging_pay': 1, 'upower_level_id': 'old-room-level'},
        )
        self.assertEqual(
            resolve_bili_upower({
                'bili_upower_enabled': 1,
                'bili_upower_level_id': 'old-room-level',
            }, {'bili_account': {'upower_level_id': 'account-level'}}),
            {'charging_pay': 1, 'upower_level_id': 'account-level'},
        )

    def test_resolve_bili_upower_skips_when_enabled_without_level(self):
        from luboman.core.bili_upower import resolve_bili_upower

        self.assertIsNone(resolve_bili_upower(
            {'bili_upower_enabled': 1},
            {'bili_account': {}},
        ))

    def test_normalize_levels_skips_six_yuan_and_short_ids(self):
        from luboman.core.bili_upower import _normalize_levels, _walk_level_dicts

        payload = {
            'data': {
                'list': [
                    {
                        'id': '952390697301177415',
                        'name': '视频补给箱',
                        'privilege_type': 20,
                        'price': 3000,
                    },
                    {
                        'id': 10,
                        'name': '为TA充电',
                        'privilege_type': 10,
                        'price': 600,
                    },
                    {
                        'privilege_id': '888888888888888888',
                        'title': '高档',
                        'privilege_type': 50,
                        'price': 12800,
                    },
                    {
                        'id': '111111111111111111',
                        'name': '为TA充电',
                        'privilege_type': 10,
                        'price': 600,
                    },
                ]
            }
        }
        levels = _normalize_levels(_walk_level_dicts(payload))
        ids = [item['id'] for item in levels]
        self.assertEqual(ids, [
            '952390697301177415',
            '888888888888888888',
            '111111111111111111',
        ])
        self.assertEqual(levels[0]['price'], 30)
        self.assertTrue(levels[0]['exclusive_ok'])
        self.assertEqual(levels[1]['price'], 128)
        self.assertFalse(levels[2]['exclusive_ok'])

    def test_fetch_upower_levels_requires_cookie(self):
        from luboman.core.bili_upower import fetch_upower_levels

        with self.assertRaises(ValueError):
            fetch_upower_levels({'id': 1})


if __name__ == "__main__":
    unittest.main()
