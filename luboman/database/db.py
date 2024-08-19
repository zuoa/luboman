import logging
import time
from datetime import datetime

from .models import LiveRoom, GlobalConfig, BiliAccount, BiliUploadTemplate, RecordFile

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
        GlobalConfig.create_table()
        LiveRoom.create_table()
        BiliAccount.create_table()
        BiliUploadTemplate.create_table()
        RecordFile.create_table()

    @classmethod
    def update_bili_upload_template(cls, data):
        update_columns = ["template_name", "bili_account_id", "tags", "description", "tid", "copyright", "cover_path", "dynamic", "dtime",
                          "dolby", "hires", "open_elec", "no_reprint", "credits", "up_selection_reply", "up_close_reply", "up_close_danmu"]
        update_data = {
            key: value for key, value in data.items() if key in update_columns
        }

        return BiliUploadTemplate.update(**update_data).where(BiliUploadTemplate.id == data["id"]).execute()

    @classmethod
    def update_live_room(cls, data):
        update_columns = ["room_name", "room_url", "custom_filename", "bili_upload_template_id", "upload_storage_platform",
                          "stream_video_format", "active_state", "active_begin", "active_end"]
        update_data = {
            key: value for key, value in data.items() if key in update_columns
        }

        update_data["gmt_updated"] = datetime.now()

        return LiveRoom.update(**update_data).where(LiveRoom.id == data["id"]).execute()

    @classmethod
    def update_live_room_operation_data(cls, data):
        update_columns = ["room_platform", "room_id", "room_title", "room_owner_id", "room_owner", "room_owner_avatar",
                          "room_owner_title", "room_cover_url", "room_cover_frame_url", "live_state", "status", "last_living_time"]
        update_data = {
            key: value for key, value in data.items() if key in update_columns
        }
        update_data["gmt_updated"] = datetime.now()

        if "room_cover_url" in update_data and update_data["room_cover_url"] == "":
            update_data.pop("room_cover_url")

        if "room_cover_frame_url" in update_data and update_data["room_cover_frame_url"] == "":
            update_data.pop("room_cover_frame_url")

        row_id = data["id"]
        return LiveRoom.update(**update_data).where(LiveRoom.id == row_id).execute()

    def backup(self):
        """备份数据库"""
        pass
