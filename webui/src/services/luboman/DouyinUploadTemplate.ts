import { request } from '@umijs/max';
import { REQUEST_HOST } from '@/constants';

/** 抖音投稿模板列表（后端返回全量） */
export async function listDouyinUploadTemplate(options?: { [key: string]: any }) {
  return request<API.DouyinUploadTemplateInfo[]>(
    REQUEST_HOST + '/v1/DouyinUploadTemplate/listAll',
    {
      method: 'POST',
      data: {},
      ...(options || {}),
    },
  );
}

/** 新建抖音投稿模板 */
export async function addDouyinUploadTemplate(
  body: API.DouyinUploadTemplateInfo,
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/DouyinUploadTemplate/add', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}

/** 更新抖音投稿模板 */
export async function updateDouyinUploadTemplate(
  body: Partial<API.DouyinUploadTemplateInfo> & { id: number },
  options?: { [key: string]: any },
) {
  return request<API.DouyinUploadTemplateInfo>(
    REQUEST_HOST + '/v1/DouyinUploadTemplate/update',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/** 删除抖音投稿模板 */
export async function delDouyinUploadTemplate(
  id: number,
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/DouyinUploadTemplate/del', {
    method: 'POST',
    data: { id },
    ...(options || {}),
  });
}
