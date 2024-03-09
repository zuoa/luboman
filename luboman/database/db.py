import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from peewee import OperationalError
from playhouse.shortcuts import model_to_dict

from .models import db, LiveRoom, GlobalConfig, get_path, BiliAccount, BiliUploadTemplate, RecordFile

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
        run = not Path(get_path('data.sqlite3')).exists()
        GlobalConfig.create_table_()
        LiveRoom.create_table_()
        BiliAccount.create_table_()
        BiliUploadTemplate.create_table_()
        RecordFile.create_table_()
        return run

    @classmethod
    def connect(cls):
        """打开数据库连接"""
        db.connect()

    @classmethod
    def close(cls):
        """关闭数据库连接"""
        db.close()

    @classmethod
    def get_stream_info(cls, name: str) -> dict:
        """根据 streamer 获取下载信息, 若不存在则返回空字典"""
        res = StreamerInfo.get_dict(name=name)
        if res:
            res["date"] = datetime_to_struct_time(res["date"])
            return res
        return {}

    @classmethod
    def get_stream_info_by_filename(cls, filename: str) -> dict:
        """通过文件名获取下载信息, 若不存在则返回空字典"""
        with db.connection_context():
            try:
                stream_info = FileList.get(FileList.file == filename).streamer_info
                stream_info_dict = model_to_dict(stream_info)
            except FileList.DoesNotExist:
                return {}
        stream_info_dict = {key: value for key, value in stream_info_dict.items() if value}  # 清除字典中的空元素
        stream_info_dict["date"] = datetime_to_struct_time(stream_info_dict["date"])  # 将开播时间转回 struct_time 类型
        return stream_info_dict

    @classmethod
    def add_stream_info(cls, name: str, url: str, date: time.struct_time) -> int:
        """添加下载信息, 返回所添加行的 id """
        return StreamerInfo.add(
            name=name,
            url=url,
            date=struct_time_to_datetime(date),
            title="",
            live_cover_path="",
        )

    @classmethod
    def delete_stream_info(cls, name: str) -> int:
        """根据 streamer 删除下载信息, 返回删除的行数, 若不存在则返回 0 """
        return StreamerInfo.delete_(name=name)

    @classmethod
    def delete_stream_info_by_date(cls, name: str, date: time.struct_time) -> int:
        """根据 streamer 和开播时间删除下载信息, 返回删除的行数, 若不存在则返回 0 """
        start_datetime = struct_time_to_datetime(date)
        with db.atomic():
            dq = StreamerInfo.delete().where(
                (StreamerInfo.name == name) &
                (StreamerInfo.date.between(  # 传入的开播时间前后一分钟内都可以匹配
                    start_datetime - timedelta(minutes=1),
                    start_datetime + timedelta(minutes=1)))
            )
            return dq.execute()

    @classmethod
    def update_cover_path(cls, database_row_id: int, live_cover_path: str):
        """更新封面存储路径"""
        if not live_cover_path:
            live_cover_path = ""
        with db.atomic():
            return StreamerInfo.update(
                live_cover_path=live_cover_path
            ).where(StreamerInfo.id == database_row_id).execute()

    @classmethod
    def update_room_title(cls, database_row_id: int, title: str):
        """更新直播标题"""
        if not title:
            title = ""
        with db.atomic():
            return StreamerInfo.update(
                title=title
            ).where(StreamerInfo.id == database_row_id).execute()

    @classmethod
    def update_file_list(cls, database_row_id: int, file_name: str) -> int:
        """向视频文件列表中添加文件名"""
        streamer_info = StreamerInfo.get_by_id_(database_row_id)
        return FileList.add(
            file=file_name,
            streamer_info=streamer_info
        )

    @classmethod
    def get_file_list(cls, database_row_id: int) -> List[str]:
        """获取视频文件列表"""
        file_list = StreamerInfo.get_by_id_(database_row_id).file_list
        return [file.file for file in file_list]

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
                          "room_owner_title", "room_cover_url", "room_cover_frame_url", "live_state", "status"]
        update_data = {
            key: value for key, value in data.items() if key in update_columns
        }
        update_data["gmt_updated"] = datetime.now()
        row_id = data["id"]
        return LiveRoom.update(**update_data).where(LiveRoom.id == row_id).execute()

    def backup(self):
        """备份数据库"""
        pass
