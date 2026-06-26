import { REQUEST_HOST } from '@/constants';
import { request } from '@umijs/max';

/** 投稿任务列表。 */
export async function listSubmissionTask(
  params: { [key: string]: any } = {},
  options?: { [key: string]: any },
) {
  return request<API.SubmissionTaskPage>(
    REQUEST_HOST + '/v1/SubmissionTask/list',
    {
      method: 'POST',
      data: params,
      ...(options || {}),
    },
  );
}

/** 投稿任务详情。 */
export async function getSubmissionTask(
  body: { id?: number; task_id?: string },
  options?: { [key: string]: any },
) {
  return request<API.SubmissionTaskInfo>(
    REQUEST_HOST + '/v1/SubmissionTask/detail',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/** 投稿任务状态统计。 */
export async function getSubmissionTaskStats(options?: { [key: string]: any }) {
  return request<API.SubmissionTaskStats>(
    REQUEST_HOST + '/v1/SubmissionTask/stats',
    {
      method: 'POST',
      data: {},
      ...(options || {}),
    },
  );
}
