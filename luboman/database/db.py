import logging
import time
from datetime import datetime

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
        """查询录像文件记录。

        服务端目前只按 live_room_id 做精确过滤（最具选择性、且与直播间强绑定）；
        room_name / platform / date / keyword / exists_only 这类需要结合磁盘扫描或
        跨表字段判断的筛选交给 Web 合并层统一处理，避免 SQL 过滤与合并层过滤语义不一致。

        每条记录会合并所属直播间的 room_name / room_platform，便于列表直接展示。
        page / page_size 为可选分页；不传时返回全部匹配记录，供 Web 层合并磁盘文件后再分页。
        返回 (records, total)，total 为匹配的数据库记录总数（分页前）。
        """
        filters = filters or {}
        with db.connection_context():
            query = RecordFile.select()
            if filters.get('live_room_id') is not None:
                query = query.where(RecordFile.live_room_id == filters['live_room_id'])

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

            return records, total

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
