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
    /** 关联的 B 站投稿模板 id（旧单值字段，仅兼容；以 bili_upload_template_ids 为准） */
    bili_upload_template_id?: number;
    /** 关联的 B 站投稿模板 id 列表（一份录播可投多个账号） */
    bili_upload_template_ids?: number[] | null;
    /** 关联的抖音投稿模板 id 列表（账号绑定在模板上） */
    douyin_upload_template_ids?: number[] | null;
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
    /** 0 关闭 / 1 开启：录制分段完成后自动探测舞蹈切片并单独投稿 */
    auto_dance_clip?: 0 | 1;
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

  type BiliAccountLoginStatus =
    | 'valid'
    | 'invalid'
    | 'missing_credentials'
    | 'disabled'
    | 'unknown';

  interface BiliAccountLoginCheckItem {
    id?: number;
    account_name?: string;
    account_avatar?: string;
    state_active?: 0 | 1;
    login_valid?: boolean | null;
    status: BiliAccountLoginStatus | string;
    message?: string;
    checked_at?: string;
  }

  interface BiliAccountLoginCheckSummary {
    active_count: number;
    invalid_count: number;
    results: BiliAccountLoginCheckItem[];
  }

  type BiliupLoginStatus =
    | 'created'
    | 'waiting'
    | 'success'
    | 'failed'
    | 'stopped'
    | 'expired';

  interface BiliupLoginSession {
    session_id: string;
    cookie_path: string;
    status: BiliupLoginStatus | string;
    /** 二维码登录 URL，前端用 QRCodeSVG 渲染；status=waiting 时有值 */
    qrcode_url?: string | null;
    error_message?: string | null;
    created_at: number;
    updated_at: number;
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

  /** 抖音账号（DouyinAccount） */
  interface DouyinAccountInfo {
    id?: number;
    account_name?: string;
    account_avatar?: string;
    /** 创作者平台扫码登录保存的 storage_state 文件路径 */
    douyin_cookies_filepath?: string;
    /** storage_state JSON 文本（备份/还原用） */
    douyin_cookies?: string;
    /** 0 停用 / 1 启用 */
    state_active?: 0 | 1;
  }

  /** 抖音扫码登录会话（二维码是 data URL，前端直接 <img> 渲染） */
  interface DouyinLoginSession {
    session_id: string;
    cookie_path: string;
    status: BiliupLoginStatus | string;
    qrcode_img?: string | null;
    error_message?: string | null;
    created_at: number;
    updated_at: number;
  }

  /** 抖音投稿模板（DouyinUploadTemplate） */
  interface DouyinUploadTemplateInfo {
    id?: number;
    template_name: string;
    douyin_account_id?: number;
    /** 标题模板，渲染后截 30 字 */
    title?: string;
    description?: string;
    /** 话题列表（不含 #） */
    tags?: string[];
    cover_path?: string;
    /** 定时发布时间戳，需距提交 2 小时~7 天内 */
    dtime?: number;
    /** 自主声明选项 */
    self_declaration?: string;
    /** 切片裁中栏转 9:16 竖屏 0/1，默认 1 */
    vertical_crop?: 0 | 1;
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
    /** nginx 静态播放地址；没有静态映射时为空 */
    stream_url?: string | null;
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
    status?: 'RECORDING' | 'COMPLETED' | string | null;
    duration_seconds?: number;
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

  /** RecordFile/publishBili、publishDouyin 响应 data：每个模板一个任务，单个失败不影响其他 */
  interface RecordFilePublishResult {
    tasks: { task_id: string; file_count: number; uploader: string }[];
    errors: {
      bili_upload_template_id?: number;
      douyin_upload_template_id?: number;
      error: string;
    }[];
  }

  type SubmissionTaskStatus =
    | 'PENDING'
    | 'RUNNING'
    | 'RETRYING'
    | 'SUCCESS'
    | 'FAILED'
    | string;

  type SubmissionTaskSource = 'AUTO' | 'FILE_MANAGER' | string;

  interface SubmissionTaskFile {
    id?: number;
    record_file_id?: number;
    video?: string;
    [key: string]: any;
  }

  /** B 站投稿任务 */
  interface SubmissionTaskInfo {
    id?: number;
    task_id: string;
    source: SubmissionTaskSource;
    platform: string;
    status: SubmissionTaskStatus;
    priority?: string;
    file_list?: SubmissionTaskFile[];
    file_count?: number;
    record_file_ids?: number[];
    live_room_id?: number | null;
    room_name?: string | null;
    room_platform?: string | null;
    bili_upload_template_id?: number | null;
    bili_upload_template_name?: string | null;
    uploader?: string | null;
    retry_count?: number;
    max_retries?: number;
    error_message?: string | null;
    result?: any | null;
    metadata?: Record<string, any> | null;
    created_at?: string;
    updated_at?: string | null;
    started_at?: string | null;
    finished_at?: string | null;
  }

  interface SubmissionTaskPage {
    list: SubmissionTaskInfo[];
    total: number;
    page: number;
  }

  interface SubmissionTaskStats {
    by_status: Record<string, number>;
    by_source: Record<string, number>;
    active: number;
    total: number;
  }

  type ClipTaskStatus = 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILED' | string;

  /** 单个来源录像的三分屏探测结果 */
  interface ClipTaskFileIntervals {
    record_file_id?: number;
    video?: string;
    /** 探测到的区间 [[start_sec, end_sec], ...] */
    intervals?: [number, number][];
    error?: string;
  }

  /** 三分屏舞蹈片段探测切片任务 */
  interface ClipTaskInfo {
    id?: number;
    task_id: string;
    status: ClipTaskStatus;
    /** MANUAL 手动探测 / AUTO 录制分段自动触发 */
    source?: 'MANUAL' | 'AUTO' | string;
    source_record_file_ids?: number[];
    record_file_count?: number;
    live_room_id?: number | null;
    room_name?: string | null;
    params?: Record<string, any> | null;
    intervals?: ClipTaskFileIntervals[] | null;
    clip_record_file_ids?: number[] | null;
    clip_count?: number;
    progress?: number;
    error_message?: string | null;
    created_at?: string;
    updated_at?: string | null;
    started_at?: string | null;
    finished_at?: string | null;
  }

  interface ClipTaskPage {
    list: ClipTaskInfo[];
    total: number;
    page: number;
  }

  interface ClipTaskStats {
    by_status: Record<string, number>;
    active: number;
    total: number;
  }

  /** RecordFile/detectDanceClip 响应 data */
  interface DetectDanceClipResult {
    task_id: string;
    file_count: number;
  }

  /** 主机级状态（System/stats → host），psutil 缺失时 available=false */
  interface SystemHostStats {
    available: boolean;
    timestamp?: number;
    cpu_percent?: number;
    memory?: {
      percent: number;
      /** 字节 */
      used: number;
      total: number;
    };
    disk?: {
      percent: number;
      used: number;
      total: number;
      free: number;
      /** 采样路径（录像目录所在盘） */
      path: string;
    };
    network?: {
      /** 上传速率，字节/秒 */
      up_rate: number;
      /** 下载速率，字节/秒 */
      down_rate: number;
      bytes_sent: number;
      bytes_recv: number;
    };
  }

  /** System/stats 响应 data（运行时状态 + 主机状态） */
  interface SystemStats {
    timestamp: number;
    host?: SystemHostStats;
    running_plugins_count?: number;
    running_plugin_ids?: string[];
    thread_pool?: Record<string, any>;
    async?: Record<string, any>;
  }
}
