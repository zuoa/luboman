import datetime
import logging
import os.path

from peewee import Model, AutoField, CharField, IntegerField, TextField, DateTimeField
try:
    from playhouse.pool import PooledPostgresqlExtDatabase
except ImportError:
    from playhouse.postgres_ext import PooledPostgresqlExtDatabase
from playhouse.sqlite_ext import JSONField

logger = logging.getLogger('luboman')


def get_current_time():
    return datetime.datetime.now()


db_host = os.environ.get('DATABASE_HOST', '10.0.4.15')
db_port = os.environ.get('DATABASE_PORT', 54321)
db_name = os.environ.get('DATABASE_NAME', 'luboman')
db_user = os.environ.get('DATABASE_USER', 'luboman')
db_password = os.environ.get('DATABASE_PASSWORD', 'luboman@2024#Hangzhou')

# 连接级超时，确保任何 DB 调用都"快速失败"而非无限挂起：
# - connect_timeout：建连阶段卡住时快速失败（秒）
# - statement_timeout：单条查询在服务端执行的超时（毫秒），慢查询快速失败
# - keepalives*：让 OS 在约 60s 内探测到被防火墙/对端悄悄掐断的死连接并快速失败，
#   避免连接池复用一个已死 socket 导致查询挂死到 OS 级 TCP 超时（十几分钟）。
# 三者缺一不可：stale_timeout 是懒回收，挡不住"死而未回收"的连接。
# peewee 会把这些额外的顶层 kwargs 连同 host/port 一起作为 psycopg2 连接参数透传，
# 切勿包在 connect_params= 字典里（会被当成名为 connect_params 的非法连接选项）。
# 全部经环境变量可覆盖。
db_connect_timeout = int(os.environ.get('DATABASE_CONNECT_TIMEOUT', 5))
db_statement_timeout = int(os.environ.get('DATABASE_STATEMENT_TIMEOUT', 15000))
db = PooledPostgresqlExtDatabase(
    db_name, host=db_host, port=db_port, user=db_user, password=db_password,
    stale_timeout=300, max_connections=100,
    connect_timeout=db_connect_timeout,
    options=f'-c statement_timeout={db_statement_timeout}',
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=3,
)


class BaseModel(Model):
    class Meta:
        database = db

    @classmethod
    def get_by_id_(cls, pk):
        with db.connection_context():
            return cls.get(cls._meta.primary_key == pk)

    @classmethod
    def create_(cls, **kwargs) -> int:
        """添加行, 返回添加的行的 id 值"""
        with db.connection_context():
            dq = cls.create(**kwargs)
            return dq.id


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
    room_cover_url = TextField(null=True)  # 直播间封面地址
    room_cover_frame_url = TextField(null=True)  # 直播间封面帧地址
    custom_filename = CharField(null=True)  # 文件名配置
    bili_upload_template_id = IntegerField(null=True)
    bili_upower_level_id = CharField(null=True)  # B站充电专属等级ID
    upload_storage_platform = CharField(null=True)  # 上传网盘类型
    stream_video_format = CharField(null=True, default="flv")  # 视频格式
    last_living_time = DateTimeField(null=True)  # 最近直播时间
    live_state = IntegerField(default=0)  # 直播状态, 0为未开播, 1为开播
    status = CharField(default='IDLE')  # 状态, IDLE为空闲, WORKING为忙碌
    gmt_created = DateTimeField(null=True, default=get_current_time)  # 创建时间
    gmt_updated = DateTimeField(null=True)  # 更新时间
    active_begin = DateTimeField(null=True)  # 活跃开始时间
    active_end = DateTimeField(null=True)  # 活跃结束时间
    active_state = IntegerField(default=1)  # 活跃状态, 0为未活跃, 1为活跃
    ffmpeg_options = JSONField(null=True)  # ffmpeg参数
    patron = CharField(null=True)  # 赞助人
    patron_link = CharField(null=True)
    notify_platform = CharField(null=True)  # 专属推送平台
    notify_token = TextField(null=True)  # 专属推送token


# 文件记录
class RecordFile(BaseModel):
    id = AutoField(primary_key=True)  # 自增主键
    live_room_id = IntegerField()
    begin_time = DateTimeField()
    end_time = DateTimeField(null=True)
    video = CharField()
    status = CharField(default='COMPLETED')  # RECORDING / COMPLETED
    duration_seconds = IntegerField(default=0)
    upload_info = JSONField(null=True)
    series_code = CharField(null=True)


class SubmissionTask(BaseModel):
    """B站投稿任务"""
    id = AutoField(primary_key=True)
    task_id = CharField(unique=True, index=True)  # 调度器任务ID
    source = CharField(default='AUTO')  # AUTO / FILE_MANAGER
    platform = CharField(default='biliweb')  # 实际上传器
    status = CharField(default='PENDING', index=True)  # PENDING / RUNNING / RETRYING / SUCCESS / FAILED
    priority = CharField(default='NORMAL')
    file_list = JSONField()
    file_count = IntegerField(default=0)
    record_file_ids = JSONField(null=True)
    live_room_id = IntegerField(null=True)
    room_name = CharField(null=True)
    room_platform = CharField(null=True)
    bili_upload_template_id = IntegerField(null=True)
    bili_upload_template_name = CharField(null=True)
    uploader = CharField(null=True)
    retry_count = IntegerField(default=0)
    max_retries = IntegerField(default=3)
    error_message = TextField(null=True)
    result = JSONField(null=True)
    metadata = JSONField(null=True)
    created_at = DateTimeField(default=get_current_time, index=True)
    updated_at = DateTimeField(null=True)
    started_at = DateTimeField(null=True)
    finished_at = DateTimeField(null=True)

# 注：(live_room_id, begin_time) 复合索引由 DB.init 用 CREATE INDEX IF NOT EXISTS 显式补建，
# 兼容已存在的库（create_table(safe=True) 不会为旧表补索引）。
