/* eslint-disable */
// 后端 luboman (aiohttp) 数据模型，对齐 luboman/database/models.py

declare namespace API {
  /** 后端统一响应包装：{ success, code, data, message } */
  interface Result<T = any> {
    success: boolean;
    code: number;
    data: T;
    message: string;
  }

  /** 全局配置项（GlobalConfig） */
  interface GlobalConfigItem {
    key: string;
    value: any;
    description?: string;
  }

  /** 直播间（LiveRoom） */
  interface LiveRoomInfo {
    id?: number;
    room_url: string;
    room_name?: string;
    room_platform?: string;
    room_id?: string;
    room_owner_id?: string;
    room_owner?: string;
    room_owner_title?: string;
    room_owner_avatar?: string;
    room_title?: string;
    room_cover_url?: string;
    room_cover_frame_url?: string;
    /** 自定义录播文件名模板 */
    custom_filename?: string;
    /** 关联的 B 站投稿模板 id */
    bili_upload_template_id?: number;
    /** B 站充电等级 id */
    bili_upower_level_id?: string;
    /** 网盘上传平台：bdpan / alipan */
    upload_storage_platform?: string;
    /** 直播流封装格式，默认 flv */
    stream_video_format?: string;
    /** 最后直播时间（后端序列化为字符串） */
    last_living_time?: string;
    /** 0 未开播 / 1 直播中 */
    live_state?: 0 | 1;
    /** IDLE / WORKING */
    status?: string;
    gmt_created?: string;
    gmt_updated?: string;
    active_begin?: string;
    active_end?: string;
    /** 0 未激活 / 1 已激活 */
    active_state?: 0 | 1;
    /** ffmpeg 额外参数（JSON 对象） */
    ffmpeg_options?: Record<string, any>;
    patron?: string;
    patron_link?: string;
    /** 通知平台：bark / pushplus / tg */
    notify_platform?: string;
    notify_token?: string;
  }

  /** B 站账号（BiliAccount） */
  interface BiliAccountInfo {
    id?: number;
    account_name?: string;
    account_avatar?: string;
    bili_cookies_filepath?: string;
    bili_cookies?: string;
    /** 0 停用 / 1 启用 */
    state_active?: 0 | 1;
  }

  /** B 站投稿模板（BiliUploadTemplate） */
  interface BiliUploadTemplateInfo {
    id?: number;
    template_name: string;
    bili_account_id?: number;
    title?: string;
    /** 投稿分区码，默认 171（电子竞技） */
    tid?: number;
    /** 1 自制 / 2 转载，默认 1 */
    copyright?: 1 | 2;
    cover_path?: string;
    description?: string;
    /** 空间动态 */
    dynamic?: string;
    /** 定时发布时间戳 */
    dtime?: number;
    /** 杜比音效 0/1 */
    dolby?: 0 | 1;
    /** Hi-Res 0/1 */
    hires?: 0 | 1;
    /** 充电面板 0/1 */
    open_elec?: 0 | 1;
    /** 自制声明 0/1 */
    no_reprint?: 0 | 1;
    tags?: string[];
    credits?: { username: string; uid: number }[];
    /** 精选评论 0/1 */
    up_selection_reply?: 0 | 1;
    /** 关闭评论 0/1 */
    up_close_reply?: 0 | 1;
    /** 关闭弹幕 0/1 */
    up_close_danmu?: 0 | 1;
    /** 单文件并发上传数，默认 3 */
    threads?: number;
    /** 上传线路：AUTO / bda2 / kodo / ws / qn / cos，默认 AUTO */
    lines?: string;
  }

  /** @/v1/bili/archive/pre 返回的 B 站原始 archive/pre 响应 */
  interface BiliArchivePre {
    code?: number;
    message?: string;
    data?: {
      type_list?: { tid: number; typename: string }[];
      [key: string]: any;
    };
    [key: string]: any;
  }

  /** 录像文件（RecordFile/list 合并 DB 记录 + 磁盘扫描后的条目） */
  interface RecordFileInfo {
    /** 磁盘补齐文件为 null */
    id?: number | null;
    /** 规范化绝对路径 */
    video: string;
    filename?: string;
    /** 字节；不在磁盘时为 null */
    size?: number | null;
    /** epoch 秒 */
    mtime?: number;
    exists?: boolean;
    source?: 'database' | 'disk';
    live_room_id?: number | null;
    room_name?: string | null;
    room_platform?: string | null;
    begin_time?: string | null;
    end_time?: string | null;
    series_code?: string | null;
    /** B 站上传结果（JSON，结构不定；未发布为 null） */
    upload_info?: any | null;
  }

  /** RecordFile/roomSummary 单个直播间维度汇总 */
  interface RecordFileRoomSummary {
    live_room_id?: number | null;
    room_name?: string | null;
    room_platform?: string | null;
    room_owner?: string | null;
    room_url?: string | null;
    live_state?: 0 | 1 | null;
    file_count: number;
    last_begin_time?: string | null;
  }

  /** RecordFile/list 响应 data */
  interface RecordFilePage {
    list: RecordFileInfo[];
    total: number;
    page: number;
  }

  /** RecordFile/publishBili 响应 data */
  interface RecordFilePublishResult {
    task_id: string;
    file_count: number;
    uploader: string;
  }
}
