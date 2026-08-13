import logging
import os
import threading
import time
from datetime import datetime, timedelta

from peewee import JOIN, fn
from playhouse.shortcuts import model_to_dict

from .models import (
    db, LiveRoom, GlobalConfig, BiliAccount, BiliUploadTemplate, RecordFile, SubmissionTask, ClipTask,
    DouyinAccount, DouyinUploadTemplate,
)

logger = logging.getLogger('luboman')

RECORD_FILE_STATUS_RECORDING = 'RECORDING'
RECORD_FILE_STATUS_COMPLETED = 'COMPLETED'

SUBMISSION_TASK_STATUS_PENDING = 'PENDING'
SUBMISSION_TASK_STATUS_RUNNING = 'RUNNING'
SUBMISSION_TASK_STATUS_RETRYING = 'RETRYING'
SUBMISSION_TASK_STATUS_SUCCESS = 'SUCCESS'
SUBMISSION_TASK_STATUS_FAILED = 'FAILED'

SUBMISSION_TASK_SOURCE_AUTO = 'AUTO'
SUBMISSION_TASK_SOURCE_FILE_MANAGER = 'FILE_MANAGER'

CLIP_TASK_STATUS_PENDING = 'PENDING'
CLIP_TASK_STATUS_RUNNING = 'RUNNING'
CLIP_TASK_STATUS_SUCCESS = 'SUCCESS'
CLIP_TASK_STATUS_FAILED = 'FAILED'


def struct_time_to_datetime(date: time.struct_time):
    return datetime.fromtimestamp(time.mktime(date))


def datetime_to_struct_time(date: datetime):
    return time.localtime(date.timestamp())


def resolve_room_template_ids(room_data, multi_key, single_key=None):
    """解析直播间绑定的投稿模板id列表。

    优先读多值字段 multi_key（列表）；为空时回退旧单值字段 single_key
    （兼容迁移前的数据与旧客户端）。去重、保序、过滤非法值。
    """
    room_data = room_data or {}
    raw = room_data.get(multi_key)
    if not raw and single_key:
        single = room_data.get(single_key)
        raw = [single] if single else []
    if not isinstance(raw, (list, tuple)):
        raw = [raw] if raw else []
    ids = []
    for item in raw:
        try:
            tid = int(item)
        except (TypeError, ValueError):
            continue
        if tid not in ids:
            ids.append(tid)
    return ids


def resolve_room_bili_template_ids(room_data):
    """解析直播间绑定的B站投稿模板id列表（全后端唯一权威读取点）。"""
    return resolve_room_template_ids(room_data, 'bili_upload_template_ids', 'bili_upload_template_id')


def resolve_room_douyin_template_ids(room_data):
    """解析直播间绑定的抖音投稿模板id列表（全后端唯一权威读取点）。"""
    return resolve_room_template_ids(room_data, 'douyin_upload_template_ids')


def should_auto_upload_full_bili(room_data) -> bool:
    """录制结束后是否自动投稿整场录像到 B 站。

    bili_upload_clips_only=1 时跳过整录，舞蹈切片仍走 auto_submit_clip_records。
    缺字段 / 0 / 非法值一律视为关，保持旧行为。
    """
    try:
        return int((room_data or {}).get('bili_upload_clips_only') or 0) != 1
    except (TypeError, ValueError):
        return True


class DB:
    """数据库交互类"""

    @classmethod
    def init(cls):
        """初始化数据库"""
        GlobalConfig.create_table(safe=True)
        LiveRoom.create_table(safe=True)
        BiliAccount.create_table(safe=True)
        BiliUploadTemplate.create_table(safe=True)
        DouyinAccount.create_table(safe=True)
        DouyinUploadTemplate.create_table(safe=True)
        RecordFile.create_table(safe=True)
        SubmissionTask.create_table(safe=True)
        ClipTask.create_table(safe=True)
        cls._ensure_record_file_schema()
        cls._ensure_live_room_schema()
        cls._ensure_live_room_bili_template_ids_schema()
        cls._ensure_live_room_douyin_template_ids_schema()
        cls._ensure_live_room_cover_schema()
        cls._ensure_bili_account_schema()
        cls._ensure_clip_task_schema()
        cls.recover_interrupted_submission_tasks()
        cls.recover_interrupted_clip_tasks()
        # 兼容已有库：补建 (live_room_id, begin_time) 复合索引（create_table 的 safe=True
        # 不会为已存在的表补索引）。大表上建索引耗时，放后台守护线程异步进行，绝不阻塞/阻断启动。
        threading.Thread(target=cls._ensure_record_file_index, daemon=True, name='recordfile-index').start()

    @classmethod
    def _ensure_record_file_schema(cls):
        """兼容旧库：补齐 RecordFile 录制状态字段，并允许 end_time 在录制中为空。"""
        table = RecordFile._meta.table_name
        try:
            with db.atomic():
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    f"ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT '{RECORD_FILE_STATUS_COMPLETED}'"
                )
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    'ADD COLUMN IF NOT EXISTS duration_seconds INTEGER NOT NULL DEFAULT 0'
                )
                db.execute_sql(f'ALTER TABLE "{table}" ALTER COLUMN end_time DROP NOT NULL')
            logger.info('RecordFile 录制状态字段就绪')
        except Exception:
            logger.warning(
                '补齐 RecordFile 录制状态字段失败，正在录制文件可能无法进入文件管理列表',
                exc_info=True,
            )

    @classmethod
    def _ensure_live_room_schema(cls):
        """兼容旧库：补齐 LiveRoom 自动舞蹈切片 / 只投稿切片开关列。"""
        table = LiveRoom._meta.table_name
        try:
            with db.atomic():
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    'ADD COLUMN IF NOT EXISTS auto_dance_clip INTEGER NOT NULL DEFAULT 0'
                )
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    'ADD COLUMN IF NOT EXISTS bili_upload_clips_only INTEGER NOT NULL DEFAULT 0'
                )
            logger.info('LiveRoom 自动舞蹈切片字段就绪')
        except Exception:
            logger.warning('补齐 LiveRoom 自动舞蹈切片字段失败', exc_info=True)

    @classmethod
    def _ensure_live_room_bili_template_ids_schema(cls):
        """兼容旧库：补齐 LiveRoom 多投稿模板列表列，并从旧单模板列回填。"""
        table = LiveRoom._meta.table_name
        try:
            with db.atomic():
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    'ADD COLUMN IF NOT EXISTS bili_upload_template_ids JSON'
                )
                db.execute_sql(
                    f'UPDATE "{table}" '
                    'SET bili_upload_template_ids = json_build_array(bili_upload_template_id) '
                    'WHERE bili_upload_template_id IS NOT NULL AND bili_upload_template_ids IS NULL'
                )
            logger.info('LiveRoom 多投稿模板字段就绪')
        except Exception:
            logger.warning('补齐 LiveRoom 多投稿模板字段失败', exc_info=True)

    @classmethod
    def _ensure_live_room_douyin_template_ids_schema(cls):
        """兼容旧库：补齐 LiveRoom 抖音投稿模板列表列（新字段，无需回填）。"""
        table = LiveRoom._meta.table_name
        try:
            with db.atomic():
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    'ADD COLUMN IF NOT EXISTS douyin_upload_template_ids JSON'
                )
            logger.info('LiveRoom 抖音投稿模板字段就绪')
        except Exception:
            logger.warning('补齐 LiveRoom 抖音投稿模板字段失败', exc_info=True)

    @classmethod
    def _ensure_live_room_cover_schema(cls):
        """兼容旧库：补齐 LiveRoom B站投稿封面设置列（新字段，无需回填）。"""
        table = LiveRoom._meta.table_name
        try:
            with db.atomic():
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    'ADD COLUMN IF NOT EXISTS cover_mode VARCHAR(16)'
                )
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    'ADD COLUMN IF NOT EXISTS custom_cover_path VARCHAR(255)'
                )
            logger.info('LiveRoom 投稿封面字段就绪')
        except Exception:
            logger.warning('补齐 LiveRoom 投稿封面字段失败', exc_info=True)

    @classmethod
    def _ensure_bili_account_schema(cls):
        """兼容旧库：补齐 BiliAccount 片头视频列（新字段，无需回填）。"""
        table = BiliAccount._meta.table_name
        try:
            with db.atomic():
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    'ADD COLUMN IF NOT EXISTS intro_video_path VARCHAR(255)'
                )
            logger.info('BiliAccount 片头字段就绪')
        except Exception:
            logger.warning('补齐 BiliAccount 片头字段失败', exc_info=True)

    @classmethod
    def _ensure_clip_task_schema(cls):
        """兼容旧库：补齐 ClipTask 任务来源列。"""
        table = ClipTask._meta.table_name
        try:
            with db.atomic():
                db.execute_sql(
                    f'ALTER TABLE "{table}" '
                    "ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'MANUAL'"
                )
            logger.info('ClipTask 任务来源字段就绪')
        except Exception:
            logger.warning('补齐 ClipTask 任务来源字段失败', exc_info=True)

    @classmethod
    def _ensure_record_file_index(cls):
        """后台建 RecordFile(live_room_id, begin_time) 索引；大表上耗时，故事务内临时关闭
        statement_timeout，并用 try/except 兜底——失败仅影响列表性能，不应让应用起不来。
        成功一次后 IF NOT EXISTS 使后续启动近乎零成本。"""
        table = RecordFile._meta.table_name
        try:
            with db.atomic():
                # SET LOCAL 仅在事务内有效，退出即回退，不会污染连接池里该连接后续的默认超时
                db.execute_sql('SET LOCAL statement_timeout = 0')
                db.execute_sql(
                    f'CREATE INDEX IF NOT EXISTS "idx_{table}_live_room_begin" '
                    f'ON "{table}" (live_room_id, begin_time)'
                )
            logger.info('RecordFile(live_room_id, begin_time) 索引就绪')
        except Exception:
            logger.warning(
                '创建 RecordFile(live_room_id, begin_time) 索引失败，'
                '录像列表仍可用但可能偏慢；表越大建索引越久，可稍后重试或手动清理死记录',
                exc_info=True,
            )
        finally:
            # 后台线程用完即归还连接，避免连接泄漏
            try:
                db.close()
            except Exception:
                pass

    @staticmethod
    def filter_model_data(model, data, allowed_columns=None, include_id=False):
        fields = set(model._meta.fields.keys())
        if allowed_columns is not None:
            fields &= set(allowed_columns)
        if not include_id:
            fields.discard("id")
        return {key: value for key, value in (data or {}).items() if key in fields}

    @classmethod
    def create_live_room(cls, data):
        with db.connection_context():
            room = LiveRoom.create(**cls.filter_model_data(LiveRoom, data))
            return model_to_dict(room)

    @classmethod
    def get_live_room_data(cls, row_id):
        with db.connection_context():
            return model_to_dict(LiveRoom.get_by_id(row_id))

    @classmethod
    def delete_live_room(cls, row_id):
        with db.connection_context():
            return LiveRoom.delete_by_id(row_id)

    @classmethod
    def create_bili_account(cls, data):
        with db.connection_context():
            account = BiliAccount.create(**cls.filter_model_data(BiliAccount, data))
            return model_to_dict(account)

    @classmethod
    def create_bili_upload_template(cls, data):
        with db.connection_context():
            template = BiliUploadTemplate.create(**cls.filter_model_data(BiliUploadTemplate, data))
            return template.id

    @classmethod
    def delete_bili_upload_template(cls, template_id):
        with db.connection_context():
            result = BiliUploadTemplate.delete_by_id(template_id)
        cls._remove_template_from_rooms(template_id)
        return result

    @classmethod
    def _remove_template_from_rooms(cls, template_id):
        """删除模板后，从所有房间的多模板列表中剔除残留id，并把旧单模板列同步为列表首元素。

        失败仅告警，不阻塞模板删除；运行时加载也会跳过失效id（见 get_bili_templates_with_accounts）。
        """
        table = LiveRoom._meta.table_name
        try:
            tid = int(template_id)
            with db.atomic():
                db.execute_sql(
                    f'UPDATE "{table}" SET bili_upload_template_ids = COALESCE('
                    f"(SELECT json_agg(e) FROM json_array_elements(bili_upload_template_ids) e "
                    f"WHERE (e #>> '{{}}')::int != {tid}), '[]'::json) "
                    f'WHERE bili_upload_template_ids IS NOT NULL '
                    f'AND bili_upload_template_ids::jsonb @> to_jsonb({tid})'
                )
                db.execute_sql(
                    f'UPDATE "{table}" '
                    f"SET bili_upload_template_id = (bili_upload_template_ids #>> '{{0}}')::int "
                    f'WHERE bili_upload_template_ids IS NOT NULL'
                )
        except Exception:
            logger.warning(f'清理房间中残留的投稿模板id失败: template_id={template_id}', exc_info=True)

    @classmethod
    def update_bili_upload_template(cls, data):
        with db.connection_context():
            update_columns = [
                "template_name", "bili_account_id", "title", "tags",
                "description", "tid", "copyright", "cover_path", "dynamic",
                "dtime", "dolby", "hires", "open_elec", "no_reprint",
                "credits", "up_selection_reply", "up_close_reply",
                "up_close_danmu", "threads", "lines"
            ]
            update_data = cls.filter_model_data(BiliUploadTemplate, data, update_columns)

            if not update_data:
                return 0

            return BiliUploadTemplate.update(**update_data).where(BiliUploadTemplate.id == data["id"]).execute()

    @classmethod
    def update_bili_account(cls, data):
        with db.connection_context():
            update_columns = [
                "account_name", "account_avatar", "bili_cookies_filepath",
                "bili_cookies", "state_active", "intro_video_path"
            ]
            update_data = cls.filter_model_data(BiliAccount, data, update_columns)

            if not update_data:
                return 0

            return BiliAccount.update(**update_data).where(BiliAccount.id == data["id"]).execute()

    @classmethod
    def create_douyin_account(cls, data):
        with db.connection_context():
            account = DouyinAccount.create(**cls.filter_model_data(DouyinAccount, data))
            return model_to_dict(account)

    @classmethod
    def update_douyin_account(cls, data):
        with db.connection_context():
            update_columns = [
                "account_name", "account_avatar", "douyin_cookies_filepath",
                "douyin_cookies", "state_active"
            ]
            update_data = cls.filter_model_data(DouyinAccount, data, update_columns)

            if not update_data:
                return 0

            return DouyinAccount.update(**update_data).where(DouyinAccount.id == data["id"]).execute()

    @classmethod
    def delete_douyin_account(cls, row_id):
        with db.connection_context():
            return DouyinAccount.delete_by_id(row_id)

    @classmethod
    def list_douyin_account(cls):
        with db.connection_context():
            return [model_to_dict(item) for item in DouyinAccount.select()]

    @classmethod
    def create_douyin_upload_template(cls, data):
        with db.connection_context():
            template = DouyinUploadTemplate.create(**cls.filter_model_data(DouyinUploadTemplate, data))
            return template.id

    @classmethod
    def update_douyin_upload_template(cls, data):
        with db.connection_context():
            update_columns = [
                "template_name", "douyin_account_id", "title", "description",
                "tags", "cover_path", "dtime", "self_declaration", "vertical_crop"
            ]
            update_data = cls.filter_model_data(DouyinUploadTemplate, data, update_columns)

            if not update_data:
                return 0

            return DouyinUploadTemplate.update(**update_data).where(DouyinUploadTemplate.id == data["id"]).execute()

    @classmethod
    def delete_douyin_upload_template(cls, template_id):
        with db.connection_context():
            result = DouyinUploadTemplate.delete_by_id(template_id)
        cls._remove_douyin_template_from_rooms(template_id)
        return result

    @classmethod
    def _remove_douyin_template_from_rooms(cls, template_id):
        """删除抖音模板后，从所有房间的抖音模板列表中剔除残留id。失败仅告警，不阻塞删除。"""
        table = LiveRoom._meta.table_name
        try:
            tid = int(template_id)
            with db.atomic():
                db.execute_sql(
                    f'UPDATE "{table}" SET douyin_upload_template_ids = COALESCE('
                    f"(SELECT json_agg(e) FROM json_array_elements(douyin_upload_template_ids) e "
                    f"WHERE (e #>> '{{}}')::int != {tid}), '[]'::json) "
                    f'WHERE douyin_upload_template_ids IS NOT NULL '
                    f'AND douyin_upload_template_ids::jsonb @> to_jsonb({tid})'
                )
        except Exception:
            logger.warning(f'清理房间中残留的抖音投稿模板id失败: template_id={template_id}', exc_info=True)

    @classmethod
    def list_douyin_upload_template(cls):
        with db.connection_context():
            return [model_to_dict(item) for item in DouyinUploadTemplate.select()]

    @classmethod
    def update_live_room(cls, data):
        with db.connection_context():
            update_columns = ["room_name", "room_url", "custom_filename", "bili_upload_template_id", "bili_upload_template_ids",
                              "douyin_upload_template_ids",
                              "upload_storage_platform",
                              "stream_video_format", "active_state", "active_begin", "active_end", "ffmpeg_options",
                              "patron", "patron_link", "notify_platform", "notify_token", "bili_upower_level_id",
                              "auto_dance_clip", "bili_upload_clips_only", "cover_mode", "custom_cover_path"]
            update_data = cls.filter_model_data(LiveRoom, data, update_columns)

            # 封面模式规范化：非法值/空串归一为 None（跟随投稿模板 cover_path）
            if "cover_mode" in update_data:
                cover_mode = update_data["cover_mode"]
                if cover_mode not in ("custom", "latest_live", "none"):
                    cover_mode = None
                update_data["cover_mode"] = cover_mode
                if cover_mode != "custom":
                    update_data["custom_cover_path"] = None
            elif "custom_cover_path" in update_data and not update_data["custom_cover_path"]:
                update_data["custom_cover_path"] = None

            # 新列表字段为权威：写入时规范化并把旧单模板列同步为首元素（旧客户端退化为"只投第一个"）
            if "bili_upload_template_ids" in update_data:
                ids = resolve_room_bili_template_ids({'bili_upload_template_ids': update_data["bili_upload_template_ids"]})
                update_data["bili_upload_template_ids"] = ids
                update_data["bili_upload_template_id"] = ids[0] if ids else None

            if "douyin_upload_template_ids" in update_data:
                update_data["douyin_upload_template_ids"] = resolve_room_douyin_template_ids(
                    {'douyin_upload_template_ids': update_data["douyin_upload_template_ids"]}
                )

            update_data["gmt_updated"] = datetime.now()

            return LiveRoom.update(**update_data).where(LiveRoom.id == data["id"]).execute()

    @classmethod
    def update_live_room_operation_data(cls, data):
        with db.connection_context():
            update_columns = ["room_platform", "room_id", "room_title", "room_owner_id", "room_owner", "room_owner_avatar",
                              "room_owner_title", "room_cover_url", "room_cover_frame_url", "live_state", "status", "last_living_time"]
            update_data = cls.filter_model_data(LiveRoom, data, update_columns)
            update_data["gmt_updated"] = datetime.now()

            if "room_cover_url" in update_data and update_data["room_cover_url"] == "":
                update_data.pop("room_cover_url")

            if "room_cover_frame_url" in update_data and update_data["room_cover_frame_url"] == "":
                update_data.pop("room_cover_frame_url")

            row_id = data["id"]
            return LiveRoom.update(**update_data).where(LiveRoom.id == row_id).execute()

    @classmethod
    def batch_update_live_rooms(cls, room_data_list):
        """批量更新直播间运行数据，按字段集合分组以减少数据库往返。"""
        update_columns = {
            "room_platform", "room_id", "room_title", "room_owner_id",
            "room_owner", "room_owner_avatar", "room_owner_title",
            "room_cover_url", "room_cover_frame_url", "live_state",
            "status", "last_living_time", "gmt_updated"
        }

        latest_by_id = {}
        for room_data in room_data_list:
            row_id = room_data.get("id")
            if not row_id:
                continue

            update_data = cls.filter_model_data(LiveRoom, room_data, update_columns)
            update_data["gmt_updated"] = datetime.now()

            if update_data.get("room_cover_url") == "":
                update_data.pop("room_cover_url")

            if update_data.get("room_cover_frame_url") == "":
                update_data.pop("room_cover_frame_url")

            if update_data:
                latest_by_id[row_id] = update_data

        if not latest_by_id:
            return 0

        grouped = {}
        for row_id, update_data in latest_by_id.items():
            fields_key = tuple(sorted(update_data.keys()))
            grouped.setdefault(fields_key, []).append((row_id, update_data))

        success_count = 0
        with db.connection_context():
            with db.atomic():
                for fields_key, rows in grouped.items():
                    fields = [getattr(LiveRoom, field_name) for field_name in fields_key]
                    models = [
                        LiveRoom(id=row_id, **update_data)
                        for row_id, update_data in rows
                    ]
                    LiveRoom.bulk_update(models, fields=fields, batch_size=100)
                    success_count += len(models)

        return success_count

    @classmethod
    def list_room(cls):
        with db.connection_context():
            res = []
            for ls in LiveRoom.select().order_by(LiveRoom.live_state.desc(), LiveRoom.last_living_time.desc()):
                temp = model_to_dict(ls)
                res.append(temp)
            return res

    @classmethod
    def list_active_rooms(cls):
        with db.connection_context():
            return [
                model_to_dict(room)
                for room in LiveRoom.select().where(LiveRoom.active_state == 1)
            ]

    @classmethod
    def deactivate_expired_rooms(cls, now=None):
        """把激活截止时间已过的直播间自动置为未激活。

        active_end 为空视为永久激活，永不到期；返回本次被停用的直播间数据列表
        （active_state 已置 0），调用方据此停止对应 worker。
        """
        now = now or datetime.now()
        with db.connection_context():
            expired = list(LiveRoom.select().where(
                (LiveRoom.active_state == 1)
                & (LiveRoom.active_end.is_null(False))
                & (LiveRoom.active_end <= now)
            ))
            if not expired:
                return []

            room_ids = [room.id for room in expired]
            LiveRoom.update(active_state=0, gmt_updated=now).where(LiveRoom.id.in_(room_ids)).execute()

            result = []
            for room in expired:
                room.active_state = 0
                result.append(model_to_dict(room))
            return result

    @classmethod
    def list_bili_account(cls):
        with db.connection_context():
            return [model_to_dict(item) for item in BiliAccount.select()]

    @classmethod
    def list_bili_upload_template(cls):
        with db.connection_context():
            return [model_to_dict(item) for item in BiliUploadTemplate.select()]

    @staticmethod
    def _record_duration_seconds(begin_time, end_time=None):
        if not begin_time:
            return 0
        end_time = end_time or datetime.now()
        try:
            return max(0, int((end_time - begin_time).total_seconds()))
        except Exception:
            return 0

    @classmethod
    def create_record_file_started(cls, data):
        """录制片段开始时立即创建文件记录，供文件管理页展示 RECORDING 状态。"""
        with db.connection_context():
            payload = cls.filter_model_data(RecordFile, data)
            payload.setdefault('begin_time', datetime.now())
            payload.setdefault('end_time', None)
            payload['status'] = RECORD_FILE_STATUS_RECORDING
            payload['duration_seconds'] = 0
            record = RecordFile.create(**payload)
            return model_to_dict(record)

    @classmethod
    def complete_record_file(cls, record_id, data=None):
        """录制片段结束时把记录标记为 COMPLETED，并固化结束时间与时长。"""
        if not record_id:
            return 0

        data = data or {}
        end_time = data.get('end_time') or datetime.now()

        with db.connection_context():
            try:
                record = RecordFile.get_by_id(record_id)
            except RecordFile.DoesNotExist:
                return 0

            update_data = cls.filter_model_data(
                RecordFile,
                data,
                allowed_columns=['video', 'end_time', 'duration_seconds', 'status', 'upload_info', 'series_code'],
            )
            update_data['end_time'] = end_time
            update_data['duration_seconds'] = cls._record_duration_seconds(record.begin_time, end_time)
            update_data['status'] = RECORD_FILE_STATUS_COMPLETED

            return (
                RecordFile.update(**update_data)
                .where(RecordFile.id == record_id)
                .execute()
            )

    @classmethod
    def cleanup_stale_recording_files(cls, timeout_seconds=3600):
        """把长时间无文件写入活动的 RECORDING 记录收敛为 COMPLETED。

        判断依据优先使用最终文件或 .part 文件的 mtime。只看 begin_time 会误伤长直播；
        mtime 超过 timeout_seconds 未变化才认为录制进程已经异常退出。
        """
        try:
            timeout_seconds = max(60, int(timeout_seconds or 3600))
        except (TypeError, ValueError):
            timeout_seconds = 3600
        now = datetime.now()
        cutoff = now - timedelta(seconds=timeout_seconds)
        completed = 0

        with db.connection_context():
            query = RecordFile.select().where(RecordFile.status == RECORD_FILE_STATUS_RECORDING)
            for record in query:
                latest_mtime = None
                for path in (record.video, f'{record.video}.part' if record.video else None):
                    if not path:
                        continue
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    latest_mtime = max(latest_mtime or mtime, mtime)

                if latest_mtime is not None:
                    latest_dt = datetime.fromtimestamp(latest_mtime)
                    if latest_dt > cutoff:
                        continue
                    end_time = latest_dt
                else:
                    if record.begin_time and record.begin_time > cutoff:
                        continue
                    end_time = now

                duration_seconds = cls._record_duration_seconds(record.begin_time, end_time)
                completed += (
                    RecordFile.update(
                        status=RECORD_FILE_STATUS_COMPLETED,
                        end_time=end_time,
                        duration_seconds=duration_seconds,
                    )
                    .where(RecordFile.id == record.id)
                    .execute()
                )

        return completed

    @classmethod
    def list_record_file(cls, filters=None, page=None, page_size=None):
        """查询录像文件记录（纯数据库驱动，过滤+排序+分页全部下推 SQL）。

        列表不再做全盘 os.walk 合并磁盘文件（曾导致请求挂死）。磁盘信息（exists/size/mtime）
        由 Web 层仅对本页结果逐个 stat 补齐。exists 过滤无法在 SQL 表达（依赖磁盘），
        也由 Web 层对本页处理；配合定期清理死记录，DB 行≈磁盘文件，分页 total 仍近似准确。

        支持 filters：live_room_id（精确）、date（YYYY-MM-DD，按 begin_time 日期）、
        room_name（LiveRoom.room_name 模糊）、platform（LiveRoom.room_platform 精确）、
        keyword（video 路径或 room_name 模糊）。room_name/platform/keyword 经 LEFT JOIN LiveRoom。
        返回 (records, total)，total 为匹配的数据库记录总数（分页前）。
        """
        filters = filters or {}
        with db.connection_context():
            query = RecordFile.select()
            if filters.get('live_room_id') is not None:
                query = query.where(RecordFile.live_room_id == filters['live_room_id'])

            date_str = (filters.get('date') or '').strip()
            if date_str:
                try:
                    day = datetime.strptime(date_str, '%Y-%m-%d')
                    day_start = datetime(day.year, day.month, day.day)
                    day_end = day_start + timedelta(days=1)
                    query = query.where(
                        (RecordFile.begin_time >= day_start) & (RecordFile.begin_time < day_end)
                    )
                except ValueError:
                    pass

            room_name = (filters.get('room_name') or '').strip()
            platform = (filters.get('platform') or '').strip()
            keyword = (filters.get('keyword') or '').strip()
            # room_name / platform / keyword(房间名) 需要关联 LiveRoom
            if room_name or platform or keyword:
                query = query.join(
                    LiveRoom, JOIN.LEFT_OUTER, on=(RecordFile.live_room_id == LiveRoom.id)
                )
                if room_name:
                    query = query.where(LiveRoom.room_name.contains(room_name))
                if platform:
                    query = query.where(LiveRoom.room_platform == platform)
                if keyword:
                    query = query.where(
                        RecordFile.video.contains(keyword) | LiveRoom.room_name.contains(keyword)
                    )

            total = query.count()
            query = query.order_by(RecordFile.begin_time.desc())
            if page and page_size:
                query = query.paginate(page, page_size)

            records = [model_to_dict(rf) for rf in query]

            room_ids = {r['live_room_id'] for r in records if r.get('live_room_id') is not None}
            room_map = {}
            if room_ids:
                for room in LiveRoom.select().where(LiveRoom.id.in_(list(room_ids))):
                    room_map[room.id] = model_to_dict(room)

            for record in records:
                room = room_map.get(record.get('live_room_id')) or {}
                record_status = record.get('status') or RECORD_FILE_STATUS_COMPLETED
                record['room_name'] = room.get('room_name')
                record['room_platform'] = room.get('room_platform')
                record['_video_real'] = os.path.realpath(record.get('video') or '')
                record['status'] = record_status
                record['duration_seconds'] = cls._record_duration_seconds(
                    record.get('begin_time'),
                    record.get('end_time') if record_status != RECORD_FILE_STATUS_RECORDING else None,
                )

            return records, total

    @classmethod
    def list_record_file_room_summary(cls):
        """按直播间汇总录像文件数量，用于文件管理页的直播间维度入口。"""
        with db.connection_context():
            summary_map = {}
            rooms = LiveRoom.select().order_by(
                LiveRoom.live_state.desc(),
                LiveRoom.last_living_time.desc(),
                LiveRoom.id.asc(),
            )
            for room in rooms:
                summary_map[room.id] = {
                    'live_room_id': room.id,
                    'room_name': room.room_name,
                    'room_platform': room.room_platform,
                    'room_owner': room.room_owner,
                    'room_url': room.room_url,
                    'live_state': room.live_state,
                    'file_count': 0,
                    'last_begin_time': None,
                }

            rows = (
                RecordFile.select(
                    RecordFile.live_room_id,
                    fn.COUNT(RecordFile.id).alias('file_count'),
                    fn.MAX(RecordFile.begin_time).alias('last_begin_time'),
                )
                .group_by(RecordFile.live_room_id)
                .dicts()
            )

            orphaned = []
            for row in rows:
                room_id = row.get('live_room_id')
                target = summary_map.get(room_id)
                if target is None:
                    target = {
                        'live_room_id': room_id,
                        'room_name': f'未关联直播间 #{room_id}' if room_id is not None else '未关联直播间',
                        'room_platform': None,
                        'room_owner': None,
                        'room_url': None,
                        'live_state': None,
                        'file_count': 0,
                        'last_begin_time': None,
                    }
                    orphaned.append(target)

                target['file_count'] = row.get('file_count') or 0
                target['last_begin_time'] = row.get('last_begin_time')

            return list(summary_map.values()) + orphaned

    @classmethod
    def delete_record_files_under_path(cls, path_prefix):
        """删除 video 路径位于 path_prefix 之下的录像记录（清理过期目录时配套清理死记录）。"""
        if not path_prefix:
            return 0
        with db.connection_context():
            return RecordFile.delete().where(RecordFile.video.startswith(path_prefix)).execute()

    @classmethod
    def create_submission_task(cls, data):
        """创建投稿任务记录。task_id 由上传调度器共享，用于后续状态回写。"""
        with db.connection_context():
            payload = cls.filter_model_data(SubmissionTask, data)
            payload.setdefault('created_at', datetime.now())
            payload.setdefault('updated_at', datetime.now())
            payload.setdefault('status', SUBMISSION_TASK_STATUS_PENDING)
            payload.setdefault('source', SUBMISSION_TASK_SOURCE_AUTO)
            file_list = payload.get('file_list') or []
            payload.setdefault('file_count', len(file_list))
            task = SubmissionTask.create(**payload)
            return model_to_dict(task)

    @classmethod
    def mark_submission_task_running(cls, task_id, retry_count=0):
        with db.connection_context():
            try:
                task = SubmissionTask.get(SubmissionTask.task_id == task_id)
            except SubmissionTask.DoesNotExist:
                return None

            task.status = SUBMISSION_TASK_STATUS_RUNNING
            task.retry_count = retry_count or 0
            task.updated_at = datetime.now()
            if task.started_at is None:
                task.started_at = task.updated_at
            task.save()
            return model_to_dict(task)

    @classmethod
    def mark_submission_task_retrying(cls, task_id, retry_count, error_message=None):
        with db.connection_context():
            try:
                task = SubmissionTask.get(SubmissionTask.task_id == task_id)
            except SubmissionTask.DoesNotExist:
                return None

            task.status = SUBMISSION_TASK_STATUS_RETRYING
            task.retry_count = retry_count or 0
            task.error_message = error_message
            task.updated_at = datetime.now()
            task.save()
            return model_to_dict(task)

    @classmethod
    def finish_submission_task(cls, task_id, success, result=None, error_message=None):
        with db.connection_context():
            try:
                task = SubmissionTask.get(SubmissionTask.task_id == task_id)
            except SubmissionTask.DoesNotExist:
                return None

            now = datetime.now()
            task.status = SUBMISSION_TASK_STATUS_SUCCESS if success else SUBMISSION_TASK_STATUS_FAILED
            task.error_message = None if success else error_message
            task.result = result
            task.updated_at = now
            task.finished_at = now
            task.save()

            record_file_ids = task.record_file_ids or []
            if success and record_file_ids:
                RecordFile.update(upload_info={
                    'task_id': task.task_id,
                    'platform': task.platform,
                    'status': SUBMISSION_TASK_STATUS_SUCCESS,
                    'finished_at': str(now),
                    'result': result,
                }).where(RecordFile.id.in_(record_file_ids)).execute()

            return model_to_dict(task)

    @classmethod
    def mark_submission_task_failed(cls, task_id, error_message, result=None):
        return cls.finish_submission_task(
            task_id,
            success=False,
            result=result,
            error_message=error_message,
        )

    @classmethod
    def recover_interrupted_submission_tasks(cls):
        """服务重启后内存上传队列不可恢复，收敛旧的未终态任务。"""
        with db.connection_context():
            now = datetime.now()
            return (
                SubmissionTask
                .update(
                    status=SUBMISSION_TASK_STATUS_FAILED,
                    error_message='service restarted before task finished',
                    updated_at=now,
                    finished_at=now,
                )
                .where(SubmissionTask.status.in_([
                    SUBMISSION_TASK_STATUS_PENDING,
                    SUBMISSION_TASK_STATUS_RUNNING,
                    SUBMISSION_TASK_STATUS_RETRYING,
                ]))
                .execute()
            )

    @classmethod
    def list_submission_task(cls, filters=None, page=None, page_size=None):
        filters = filters or {}
        with db.connection_context():
            query = SubmissionTask.select()

            status = (filters.get('status') or '').strip()
            if status:
                query = query.where(SubmissionTask.status == status)

            source = (filters.get('source') or '').strip()
            if source:
                query = query.where(SubmissionTask.source == source)

            platform = (filters.get('platform') or '').strip()
            if platform:
                query = query.where(SubmissionTask.platform == platform)

            live_room_id = filters.get('live_room_id')
            if live_room_id is not None:
                query = query.where(SubmissionTask.live_room_id == live_room_id)

            keyword = (filters.get('keyword') or '').strip()
            if keyword:
                query = query.where(
                    SubmissionTask.task_id.contains(keyword) |
                    SubmissionTask.room_name.contains(keyword) |
                    SubmissionTask.uploader.contains(keyword) |
                    SubmissionTask.bili_upload_template_name.contains(keyword)
                )

            total = query.count()
            query = query.order_by(SubmissionTask.created_at.desc())
            if page and page_size:
                query = query.paginate(page, page_size)

            return [model_to_dict(item) for item in query], total

    @classmethod
    def get_submission_task(cls, task_id=None, row_id=None):
        with db.connection_context():
            if row_id:
                return model_to_dict(SubmissionTask.get_by_id(row_id))
            if task_id:
                return model_to_dict(SubmissionTask.get(SubmissionTask.task_id == task_id))
            raise ValueError('task_id or id is required')

    @classmethod
    def get_submission_task_stats(cls):
        with db.connection_context():
            by_status = {
                row['status']: row['count']
                for row in (
                    SubmissionTask
                    .select(SubmissionTask.status, fn.COUNT(SubmissionTask.id).alias('count'))
                    .group_by(SubmissionTask.status)
                    .dicts()
                )
            }
            by_source = {
                row['source']: row['count']
                for row in (
                    SubmissionTask
                    .select(SubmissionTask.source, fn.COUNT(SubmissionTask.id).alias('count'))
                    .group_by(SubmissionTask.source)
                    .dicts()
                )
            }
            active = sum(by_status.get(status, 0) for status in (
                SUBMISSION_TASK_STATUS_PENDING,
                SUBMISSION_TASK_STATUS_RUNNING,
                SUBMISSION_TASK_STATUS_RETRYING,
            ))
            return {
                'by_status': by_status,
                'by_source': by_source,
                'active': active,
                'total': sum(by_status.values()),
            }

    @classmethod
    def get_first_bili_account(cls):
        with db.connection_context():
            return BiliAccount.select().where(BiliAccount.state_active == 1).first()

    # ---------------- 三分屏探测切片任务 ----------------

    @classmethod
    def create_clip_task(cls, data):
        """创建切片任务记录。task_id 由调度器共享，用于后续状态回写。"""
        with db.connection_context():
            payload = cls.filter_model_data(ClipTask, data)
            payload.setdefault('created_at', datetime.now())
            payload.setdefault('updated_at', datetime.now())
            payload.setdefault('status', CLIP_TASK_STATUS_PENDING)
            ids = payload.get('source_record_file_ids') or []
            payload.setdefault('record_file_count', len(ids))
            task = ClipTask.create(**payload)
            return model_to_dict(task)

    @classmethod
    def mark_clip_task_running(cls, task_id):
        with db.connection_context():
            try:
                task = ClipTask.get(ClipTask.task_id == task_id)
            except ClipTask.DoesNotExist:
                return None

            task.status = CLIP_TASK_STATUS_RUNNING
            task.updated_at = datetime.now()
            if task.started_at is None:
                task.started_at = task.updated_at
            task.save()
            return model_to_dict(task)

    @classmethod
    def update_clip_task_progress(cls, task_id, progress, intervals=None):
        """按文件粒度回写进度与已探测区间。"""
        with db.connection_context():
            try:
                task = ClipTask.get(ClipTask.task_id == task_id)
            except ClipTask.DoesNotExist:
                return None

            task.progress = max(0, min(100, int(progress)))
            if intervals is not None:
                task.intervals = intervals
            task.updated_at = datetime.now()
            task.save()
            return model_to_dict(task)

    @classmethod
    def finish_clip_task(cls, task_id, success, clip_record_file_ids=None, intervals=None,
                         error_message=None, only_if_active=False):
        with db.connection_context():
            try:
                task = ClipTask.get(ClipTask.task_id == task_id)
            except ClipTask.DoesNotExist:
                return None

            # only_if_active：看门狗/重试已把任务收敛到终态后，不允许迟到的回写覆盖
            if only_if_active and task.status not in (
                CLIP_TASK_STATUS_PENDING, CLIP_TASK_STATUS_RUNNING,
            ):
                return model_to_dict(task)

            now = datetime.now()
            task.status = CLIP_TASK_STATUS_SUCCESS if success else CLIP_TASK_STATUS_FAILED
            task.error_message = None if success else error_message
            if clip_record_file_ids is not None:
                task.clip_record_file_ids = clip_record_file_ids
                task.clip_count = len(clip_record_file_ids)
            if intervals is not None:
                task.intervals = intervals
            if success:
                task.progress = 100
            task.updated_at = now
            task.finished_at = now
            task.save()
            return model_to_dict(task)

    @classmethod
    def list_stale_running_clip_tasks(cls, timeout_hours):
        """RUNNING 且 updated_at 超过 timeout_hours 未刷新的任务（卡死嫌疑），返回 task_id 列表。"""
        with db.connection_context():
            deadline = datetime.now() - timedelta(hours=float(timeout_hours))
            rows = (
                ClipTask
                .select(ClipTask.task_id)
                .where(
                    (ClipTask.status == CLIP_TASK_STATUS_RUNNING) &
                    (ClipTask.updated_at.is_null(False)) &
                    (ClipTask.updated_at < deadline)
                )
            )
            return [row.task_id for row in rows]

    @classmethod
    def reset_clip_task_for_retry(cls, task_id):
        """把失败任务重置为 PENDING 供重新排队。非失败态拒绝重置（防止重复执行）。"""
        with db.connection_context():
            try:
                task = ClipTask.get(ClipTask.task_id == task_id)
            except ClipTask.DoesNotExist:
                raise ValueError(f'切片任务不存在: {task_id}')
            if task.status != CLIP_TASK_STATUS_FAILED:
                raise ValueError(f'仅失败的任务可重试（当前状态: {task.status}）')
            task.status = CLIP_TASK_STATUS_PENDING
            task.progress = 0
            task.error_message = None
            task.started_at = None
            task.finished_at = None
            task.updated_at = datetime.now()
            task.save()
            return model_to_dict(task)

    @classmethod
    def recover_interrupted_clip_tasks(cls):
        """服务重启后内存队列不可恢复，收敛旧的未终态切片任务。"""
        with db.connection_context():
            now = datetime.now()
            return (
                ClipTask
                .update(
                    status=CLIP_TASK_STATUS_FAILED,
                    error_message='service restarted before task finished',
                    updated_at=now,
                    finished_at=now,
                )
                .where(ClipTask.status.in_([
                    CLIP_TASK_STATUS_PENDING,
                    CLIP_TASK_STATUS_RUNNING,
                ]))
                .execute()
            )

    @classmethod
    def get_record_file(cls, row_id):
        with db.connection_context():
            return model_to_dict(RecordFile.get_by_id(row_id))

    @classmethod
    def get_bili_template_with_account(cls, template_id):
        """加载投稿模板及其绑定的账号，返回附带 bili_account 的模板字典。"""
        with db.connection_context():
            template = BiliUploadTemplate.get_by_id(template_id)
            if template.bili_account_id is None:
                raise ValueError('bili_upload_template has no bili_account_id')
            account = BiliAccount.get_by_id(template.bili_account_id)
            template_info = model_to_dict(template)
            template_info['bili_account'] = model_to_dict(account)
            return template_info

    @classmethod
    def get_bili_templates_with_accounts(cls, template_ids):
        """批量加载投稿模板及其绑定账号；单个失效（不存在/无账号）记 warning 跳过，不拖死整批。"""
        templates = []
        for template_id in template_ids or []:
            try:
                templates.append(cls.get_bili_template_with_account(template_id))
            except Exception:
                logger.warning(f'加载B站投稿模板失败，已跳过: template_id={template_id}', exc_info=True)
        return templates

    @classmethod
    def get_douyin_template_with_account(cls, template_id):
        """加载抖音投稿模板及其绑定的账号，返回附带 douyin_account 的模板字典。"""
        with db.connection_context():
            template = DouyinUploadTemplate.get_by_id(template_id)
            if template.douyin_account_id is None:
                raise ValueError('douyin_upload_template has no douyin_account_id')
            account = DouyinAccount.get_by_id(template.douyin_account_id)
            template_info = model_to_dict(template)
            template_info['douyin_account'] = model_to_dict(account)
            return template_info

    @classmethod
    def get_douyin_templates_with_accounts(cls, template_ids):
        """批量加载抖音投稿模板及其绑定账号；单个失效（不存在/无账号）记 warning 跳过，不拖死整批。"""
        templates = []
        for template_id in template_ids or []:
            try:
                templates.append(cls.get_douyin_template_with_account(template_id))
            except Exception:
                logger.warning(f'加载抖音投稿模板失败，已跳过: template_id={template_id}', exc_info=True)
        return templates

    @classmethod
    def list_clip_task(cls, filters=None, page=None, page_size=None):
        filters = filters or {}
        with db.connection_context():
            query = ClipTask.select()

            status = (filters.get('status') or '').strip()
            if status:
                query = query.where(ClipTask.status == status)

            live_room_id = filters.get('live_room_id')
            if live_room_id is not None:
                query = query.where(ClipTask.live_room_id == live_room_id)

            keyword = (filters.get('keyword') or '').strip()
            if keyword:
                query = query.where(
                    ClipTask.task_id.contains(keyword) |
                    ClipTask.room_name.contains(keyword)
                )

            total = query.count()
            query = query.order_by(ClipTask.created_at.desc())
            if page and page_size:
                query = query.paginate(page, page_size)

            return [model_to_dict(item) for item in query], total

    @classmethod
    def get_clip_task(cls, task_id=None, row_id=None):
        with db.connection_context():
            if row_id:
                return model_to_dict(ClipTask.get_by_id(row_id))
            if task_id:
                return model_to_dict(ClipTask.get(ClipTask.task_id == task_id))
            raise ValueError('task_id or id is required')

    @classmethod
    def get_clip_task_stats(cls):
        with db.connection_context():
            by_status = {
                row['status']: row['count']
                for row in (
                    ClipTask
                    .select(ClipTask.status, fn.COUNT(ClipTask.id).alias('count'))
                    .group_by(ClipTask.status)
                    .dicts()
                )
            }
            active = sum(by_status.get(status, 0) for status in (
                CLIP_TASK_STATUS_PENDING,
                CLIP_TASK_STATUS_RUNNING,
            ))
            return {
                'by_status': by_status,
                'active': active,
                'total': sum(by_status.values()),
            }

    @classmethod
    def upsert_clip_record_file(cls, data):
        """按 video 路径查重插入/更新切片文件记录，返回 (record_dict, created)。"""
        video = data.get('video')
        if not video:
            raise ValueError('video is required')
        with db.connection_context():
            payload = cls.filter_model_data(RecordFile, data)
            payload['status'] = RECORD_FILE_STATUS_COMPLETED
            try:
                record = RecordFile.get(RecordFile.video == video)
            except RecordFile.DoesNotExist:
                record = RecordFile.create(**payload)
                return model_to_dict(record), True

            for key, value in payload.items():
                setattr(record, key, value)
            record.save()
            return model_to_dict(record), False

    @classmethod
    def load_config(cls):
        with db.connection_context():
            res = {}
            for ls in GlobalConfig.select():
                res[ls.key] = ls.value

            return res

    def backup(self):
        """备份数据库"""
        pass
