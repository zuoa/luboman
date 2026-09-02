import { REQUEST_HOST } from '@/constants';
import { request } from '@umijs/max';

/**
 * 录像文件列表（DB 记录分页，后端仅对当前页补齐磁盘状态）。
 * params：page / page_size / live_room_id / room_name(模糊) / platform(精确) /
 *         date / keyword / exists_only(默认 true，仅磁盘存在)。
 */
export async function listRecordFile(
  params: { [key: string]: any } = {},
  options?: { [key: string]: any },
) {
  return request<API.RecordFilePage>(REQUEST_HOST + '/v1/RecordFile/list', {
    method: 'POST',
    data: params,
    // 录像列表重建（磁盘扫描 + 全表加载）可能较慢，单独放宽；调用方可经 options.timeout 覆盖
    timeout: 30000,
    ...(options || {}),
  });
}

/** 按直播间汇总录像文件数量，用于文件管理页的直播间维度入口。 */
export async function listRecordFileRoomSummary(
  params: { [key: string]: any } = {},
  options?: { [key: string]: any },
) {
  return request<API.RecordFileRoomSummary[]>(
    REQUEST_HOST + '/v1/RecordFile/roomSummary',
    {
      method: 'POST',
      data: params,
      ...(options || {}),
    },
  );
}

/**
 * 录像文件在线播放地址。后端同时由 aiohttp 直接提供 /video/ 静态文件（FileResponse
 * 支持 Range），因此统一经 /api 前缀回源到后端播放——不再依赖 nginx /video 静态
 * location，单容器 / 本地 dev / compose 部署均可播放（nginx 若配有 /video 也兼容）。
 */
export function getRecordFileStreamUrl(record: API.RecordFileInfo | number) {
  if (typeof record === 'number') {
    return `${REQUEST_HOST}/v1/RecordFile/stream/${record}`;
  }
  if (record.stream_url) {
    return `${REQUEST_HOST}${record.stream_url}`;
  }
  return record.id != null
    ? `${REQUEST_HOST}/v1/RecordFile/stream/${record.id}`
    : undefined;
}

/**
 * 录像文件下载地址。固定走 stream 接口 + download=1（后端切 attachment），
 * 不能复用 /video/ 静态路径——静态服务加 query 不会改变 Content-Disposition。
 */
export function getRecordFileDownloadUrl(record: API.RecordFileInfo | number) {
  const id = typeof record === 'number' ? record : record.id;
  return id != null
    ? `${REQUEST_HOST}/v1/RecordFile/stream/${id}?download=1`
    : undefined;
}

/**
 * 手动发布录像到 B 站（异步排队上传，每个模板创建一个任务，立即返回任务列表）。
 * body：bili_upload_template_ids(必填，可多选投多个账号) + (file_ids | videos 二选一) +
 *       可选 live_room_id、room_data(仅 room_name/title/url/owner/platform 生效)、
 *       reset_timestamps(投稿前重置音频/视频时间戳，修复直播录像 ts 跳变导致的审核失败)、
 *       bili_upower_enabled(本次是否发充电专属；档位用投稿账号上选的)。
 */
export async function publishRecordFileToBili(
  body: {
    bili_upload_template_ids: number[];
    file_ids?: number[];
    videos?: string[];
    live_room_id?: number;
    room_data?: Record<string, any>;
    reset_timestamps?: boolean;
    bili_upower_enabled?: boolean | 0 | 1;
  },
  options?: { [key: string]: any },
) {
  return request<API.RecordFilePublishResult>(
    REQUEST_HOST + '/v1/RecordFile/publishBili',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/**
 * 手动发布录像到抖音（异步排队发布，每个模板创建一个任务，立即返回任务列表）。
 * 切片按模板 vertical_crop 裁竖屏，整录仅转码 mp4；抖音限 ≤4G/≤15 分钟。
 * body：douyin_upload_template_ids(必填) + (file_ids | videos 二选一) +
 *       可选 live_room_id、room_data(仅 room_name/title/url/owner/platform 生效)。
 */
export async function publishRecordFileToDouyin(
  body: {
    douyin_upload_template_ids: number[];
    file_ids?: number[];
    videos?: string[];
    live_room_id?: number;
    room_data?: Record<string, any>;
  },
  options?: { [key: string]: any },
) {
  return request<API.RecordFilePublishResult>(
    REQUEST_HOST + '/v1/RecordFile/publishDouyin',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/**
 * 手动上传录像到网盘（异步排队，立即返回任务）。
 * body：upload_storage_platform(quark/alipan/bdpan，可省略则回退房间配置)
 *       + (file_ids | videos 二选一) + 可选 live_room_id。
 */
export async function publishRecordFileToStorage(
  body: {
    upload_storage_platform?: 'quark' | 'alipan' | 'bdpan';
    file_ids?: number[];
    videos?: string[];
    live_room_id?: number;
  },
  options?: { [key: string]: any },
) {
  return request<API.RecordFilePublishResult>(
    REQUEST_HOST + '/v1/RecordFile/publishStorage',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}
