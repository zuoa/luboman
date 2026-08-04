import { request } from '@umijs/max';
import { REQUEST_HOST } from '@/constants';

/** 直播间列表（后端返回全量，忽略分页/搜索参数） */
export async function listLiveRoom(options?: { [key: string]: any }) {
  return request<API.LiveRoomInfo[]>(REQUEST_HOST + '/v1/LiveRoom/listAll', {
    method: 'POST',
    data: {},
    ...(options || {}),
  });
}

/** 新建直播间（后端会自动开始录制） */
export async function addLiveRoom(
  body: API.LiveRoomInfo,
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/LiveRoom/add', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}

/** 更新直播间（后端会刷新运行中的录制插件） */
export async function updateLiveRoom(
  body: Partial<API.LiveRoomInfo> & { id: number },
  options?: { [key: string]: any },
) {
  return request<API.LiveRoomInfo>(REQUEST_HOST + '/v1/LiveRoom/update', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}

/** 删除直播间（后端会停止录制） */
export async function deleteLiveRoom(
  id: number,
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/LiveRoom/del', {
    method: 'POST',
    data: { id },
    ...(options || {}),
  });
}

/** 探测直播间：按链接抓取平台/房间名/标题/开播状态，用于表单自动回填 */
export async function probeLiveRoom(
  roomUrl: string,
  options?: { [key: string]: any },
) {
  return request<API.LiveRoomProbeResult>(
    REQUEST_HOST + '/v1/LiveRoom/probe',
    {
      method: 'POST',
      data: { room_url: roomUrl },
      ...(options || {}),
    },
  );
}
