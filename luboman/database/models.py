import datetime
import logging
import os.path
from pathlib import Path
from sqlite3 import IntegrityError

from peewee import Model, AutoField, CharField, IntegerField, TextField, ForeignKeyField, CompositeKey, DateTimeField
from playhouse.shortcuts import ReconnectMixin, model_to_dict
from playhouse.sqlite_ext import SqliteExtDatabase, JSONField

logger = logging.getLogger('luboman')


def get_path(*other):
    """获取数据文件绝对路径"""
    dir_path = "/data/db" if os.path.exists('/.dockerenv') else 'data/db'
    # 若目录不存在则创建
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    return os.path.join(dir_path, *other)


# 自动重连, 避免报错导致连接丢失
class ReconnectSqliteDatabase(ReconnectMixin, SqliteExtDatabase):
    pass


db = ReconnectSqliteDatabase(f"{get_path('data.sqlite3')}")


class BaseModel(Model):
    class Meta:
        database = db

    @classmethod
    def add(cls, **kwargs) -> int:
        """添加行, 返回添加的行的 id 值"""
        with db.atomic():
            dq = cls.create(**kwargs)
            return dq.id

    @classmethod
    def delete_(cls, **kwargs):
        """删除行"""
        with db.atomic():
            try:
                query = cls.get(**kwargs)
                return query.delete_instance()
            except cls.DoesNotExist:
                return 0

    @classmethod
    def create_table_(cls):
        """创建表"""
        with db.atomic():
            if not cls.table_exists():
                cls.create_table()

    @classmethod
    def get_by_id_(cls, pk):
        """根据主键获取记录"""
        with db.connection_context():
            try:
                return cls.get_by_id(pk)
            except cls.DoesNotExist:
                return cls()  # 若不存在, 则返回一个空对象

    @classmethod
    def get_dict(cls, **kwargs):
        """获取字典类型的数据"""
        with db.connection_context():
            try:
                obj = cls.get(**kwargs)
                return model_to_dict(obj)
            except cls.DoesNotExist:
                return {}


class GlobalConfig(BaseModel):
    """配置表"""
    id = AutoField(primary_key=True)  # 自增主键
    key = CharField(unique=True)  # 配置名称
    value = TextField()  # 配置值
    description = TextField(null=True)  # 配置描述


class BiliAccount(BaseModel):
    """B站账号"""
    id = AutoField(primary_key=True)  # 自增主键
    account_name = CharField(null=True)  # 账号名称
    account_avatar = CharField(null=True)  # 头像地址
    bili_cookies_filepath = CharField(null=True)  # B站cookie文件路径
    bili_cookies = TextField(null=True)  # B站cookie
    state_active = IntegerField(default=1)  # 状态, 0为未激活, 1为激活


class BiliUploadTemplate(BaseModel):
    """投稿模板"""
    id = AutoField(primary_key=True)  # 自增主键
    template_name = CharField()  # 模板名称
    bili_account_id = IntegerField()  # B站账号ID
    title = CharField(null=True)  # 自定义标题的时间格式, {title}代表当场直播间标题 {streamer}代表在本config里面设置的主播名称 {url}代表设置的该主播的第一条直播间链接
    tid = IntegerField(null=True, default=171)  # 投稿分区码,171为电子竞技分区
    copyright = IntegerField(null=True, default=1)  # 1为自制
    cover_path = CharField(null=True)  # 封面路径
    # 支持strftime, {title}, {streamer}, {url}占位符。
    description = TextField(null=True)  # 视频简介
    dynamic = CharField(null=True)  # 粉丝动态
    dtime = IntegerField(null=True)  # 设置延时发布时间，距离提交大于2小时，格式为时间戳
    dolby = IntegerField(null=True)  # 是否开启杜比音效, 1为开启
    hires = IntegerField(null=True)  # 是否开启Hi-Res, 1为开启
    open_elec = IntegerField(null=True)  # 是否开启充电面板, 1为开启
    no_reprint = IntegerField(null=True)  # 自制声明, 1为未经允许禁止转载
    tags = JSONField()  # 标签
    credits = JSONField(null=True)  # 简介@模板
    up_selection_reply = IntegerField(null=True)  # 精选评论
    up_close_reply = IntegerField(null=True)  # 关闭评论
    up_close_danmu = IntegerField(null=True)  # 精选评论
    threads = IntegerField(null=True, default=3)  # 线程数
    lines = CharField(null=True, default='AUTO')  # 线路


class LiveRoom(BaseModel):
    """每个直播间的配置"""
    id = AutoField(primary_key=True)  # 自增主键
    room_url = CharField(unique=True)  # 直播间地址
    room_name = CharField()  # 直播间名称
    room_platform = CharField(null=True)  # 直播平台
    room_id = CharField(null=True)  # 直播间 id
    room_owner_id = CharField(null=True)  # 主播ID
    room_owner = CharField(null=True)  # 主播名称
    room_owner_title = CharField(null=True)  # 主播头衔
    room_owner_avatar = CharField(null=True)  # 主播头像
    room_title = CharField(null=True)  # 直播间标题
    room_cover_url = CharField(null=True)  # 直播间封面地址
    room_cover_frame_url = CharField(null=True)  # 直播间封面帧地址
    custom_filename = CharField(null=True)  # 文件名配置
    bili_upload_template_id = IntegerField(null=True)
    upload_storage_platform = CharField(null=True)  # 上传网盘类型
    stream_video_format = CharField(null=True, default="flv")  # 视频格式
    live_state = IntegerField(default=0)  # 直播状态, 0为未开播, 1为开播
    status = CharField(default='IDLE')  # 状态, IDLE为空闲, WORKING为忙碌
    gmt_created = DateTimeField(null=True, default=datetime.datetime.now)  # 创建时间
    gmt_updated = DateTimeField(null=True)  # 更新时间
    active_begin = DateTimeField(null=True)  # 活跃开始时间
    active_end = DateTimeField(null=True)  # 活跃结束时间
    active_state = IntegerField(default=1)  # 活跃状态, 0为未活跃, 1为活跃
    message_notify_token = CharField(null=True)  # 通知机器人的token
    ffmpeg_options = JSONField(null=True)  # ffmpeg参数
