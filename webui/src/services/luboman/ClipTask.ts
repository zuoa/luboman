import { REQUEST_HOST } from '@/constants';
import { request } from '@umijs/max';

/**
 * 探测录像中的三分屏舞蹈片段并自动切片（异步排队，立即返回 task_id）。
 * body：file_ids(必填，录像记录 id 列表) + 可选 params（覆盖探测参数）。
 */
export async function detectDanceClip(
  body: {
    file_ids: number[];
    params?: Record<string, any>;
  },
  options?: { [key: string]: any },
) {
  return request<API.DetectDanceClipResult>(
    REQUEST_HOST + '/v1/RecordFile/detectDanceClip',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/** 切片任务列表。 */
export async function listClipTask(
  params: { [key: string]: any } = {},
  options?: { [key: string]: any },
) {
  return request<API.ClipTaskPage>(REQUEST_HOST + '/v1/ClipTask/list', {
    method: 'POST',
    data: params,
    ...(options || {}),
  });
}

/** 切片任务详情。 */
export async function getClipTask(
  body: { id?: number; task_id?: string },
  options?: { [key: string]: any },
) {
  return request<API.ClipTaskInfo>(REQUEST_HOST + '/v1/ClipTask/detail', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}

/** 切片任务状态统计。 */
export async function getClipTaskStats(options?: { [key: string]: any }) {
  return request<API.ClipTaskStats>(REQUEST_HOST + '/v1/ClipTask/stats', {
    method: 'POST',
    data: {},
    ...(options || {}),
  });
}

/** 重试失败的切片任务（沿用原任务 ID 与参数快照重新排队）。 */
export async function retryClipTask(
  body: { task_id: string },
  options?: { [key: string]: any },
) {
  return request(REQUEST_HOST + '/v1/ClipTask/retry', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}
