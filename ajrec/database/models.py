import logging
from pathlib import Path
from sqlite3 import IntegrityError

from peewee import Model, AutoField, CharField, IntegerField, TextField, ForeignKeyField, CompositeKey
from playhouse.shortcuts import ReconnectMixin, model_to_dict
from playhouse.sqlite_ext import SqliteExtDatabase, JSONField

logger = logging.getLogger('ajrec')


def get_path(*other):
    """获取数据文件绝对路径"""
    dir_path = Path.cwd().joinpath("data")
    # 若目录不存在则创建
    if not dir_path.is_dir():
        dir_path.mkdir(parents=True)
    return str(dir_path.joinpath(*other))


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


class BiliUploadTemplate(BaseModel):
    """投稿模板"""
    id = AutoField(primary_key=True)  # 自增主键
    template_name = CharField()  # 模板名称
    title = CharField(null=True)  # 自定义标题的时间格式, {title}代表当场直播间标题 {streamer}代表在本config里面设置的主播名称 {url}代表设置的该主播的第一条直播间链接
    tid = IntegerField(null=True)  # 投稿分区码,171为电子竞技分区
    copyright = IntegerField(null=True)  # 1为自制
    cover_path = CharField(null=True)  # 封面路径
    # 支持strftime, {title}, {streamer}, {url}占位符。
    description = TextField(null=True)  # 视频简介
    dynamic = CharField(null=True)  # 粉丝动态
    dtime = IntegerField(null=True)  # 设置延时发布时间，距离提交大于2小时，格式为时间戳
    dolby = IntegerField(null=True)  # 是否开启杜比音效, 1为开启
    hires = IntegerField(null=True)  # 是否开启Hi-Res, 1为开启
    open_elec = IntegerField(null=True)  # 是否开启充电面板, 1为开启
    no_reprint = IntegerField(null=True)  # 自制声明, 1为未经允许禁止转载
    user_cookie = CharField(null=True)  # 使用指定的账号上传
    tags = JSONField()  # 标签
    credits = JSONField(null=True)  # 简介@模板
    up_selection_reply = IntegerField(null=True)  # 精选评论
    up_close_reply = IntegerField(null=True)  # 关闭评论
    up_close_danmu = IntegerField(null=True)  # 精选评论


class LiveRoom(BaseModel):
    """每个直播间的配置"""
    id = AutoField(primary_key=True)  # 自增主键
    room_url = CharField(unique=True)  # 直播间地址
    room_name = CharField()  # 直播间名称
    room_platform = CharField(null=True)  # 直播平台
    room_id = CharField(null=True)  # 直播间 id
    room_owner_id = CharField(null=True)  # 主播ID
    room_owner = CharField(null=True)  # 主播名称
    room_owner_avatar = CharField(null=True)  # 主播头像
    room_title = CharField(null=True)  # 直播间标题
    room_cover_url = CharField(null=True)  # 直播间封面地址
    room_cover_frame_url = CharField(null=True)  # 直播间封面帧地址
    custom_filename = CharField(null=True)  # 文件名配置
    # 外键, 对应 UploadStreamers, 且启用级联删除
    upload_template_id = IntegerField(null=True)
    stream_video_format = CharField(null=True)  # 视频格式
    live_state = IntegerField(default=0)  # 直播状态, 0为未开播, 1为开播
    status = CharField(default='IDLE')  # 状态, IDLE为空闲, WORKING为忙碌
