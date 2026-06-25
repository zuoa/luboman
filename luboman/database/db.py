import logging
import os
import threading
import time
from datetime import datetime, timedelta

from peewee import JOIN, fn
from playhouse.shortcuts import model_to_dict

from .models import db, LiveRoom, GlobalConfig, BiliAccount, BiliUploadTemplate, RecordFile

logger = logging.getLogger('luboman')


def struct_time_to_datetime(date: time.struct_time):
    return datetime.fromtimestamp(time.mktime(date))


def datetime_to_struct_time(date: datetime):
    return time.localtime(date.timestamp())


class DB:
    """数据库交互类"""

    @classmethod
    def init(cls):
        """初始化数据库"""
        GlobalConfig.create_table(safe=True)
        LiveRoom.create_table(safe=True)
        BiliAccount.create_table(safe=True)
        BiliUploadTemplate.create_table(safe=True)
        RecordFile.create_table(safe=True)
        # 兼容已有库：补建 (live_room_id, begin_time) 复合索引（create_table 的 safe=True
        # 不会为已存在的表补索引）。大表上建索引耗时，放后台守护线程异步进行，绝不阻塞/阻断启动。
        threading.Thread(target=cls._ensure_record_file_index, daemon=True, name='recordfile-index').start()

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
            return BiliUploadTemplate.delete_by_id(template_id)

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
                "bili_cookies", "state_active"
            ]
            update_data = cls.filter_model_data(BiliAccount, data, update_columns)

            if not update_data:
                return 0

            return BiliAccount.update(**update_data).where(BiliAccount.id == data["id"]).execute()

    @classmethod
    def update_live_room(cls, data):
        with db.connection_context():
            update_columns = ["room_name", "room_url", "custom_filename", "bili_upload_template_id", "upload_storage_platform",
                              "stream_video_format", "active_state", "active_begin", "active_end", "ffmpeg_options",
                              "patron", "patron_link", "notify_platform", "notify_token", "bili_upower_level_id"]
            update_data = cls.filter_model_data(LiveRoom, data, update_columns)

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
    def list_bili_account(cls):
        with db.connection_context():
            return [model_to_dict(item) for item in BiliAccount.select()]

    @classmethod
    def list_bili_upload_template(cls):
        with db.connection_context():
            return [model_to_dict(item) for item in BiliUploadTemplate.select()]

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
                record['room_name'] = room.get('room_name')
                record['room_platform'] = room.get('room_platform')
                record['_video_real'] = os.path.realpath(record.get('video') or '')

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
    def get_first_bili_account(cls):
        with db.connection_context():
            return BiliAccount.select().where(BiliAccount.state_active == 1).first()


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
