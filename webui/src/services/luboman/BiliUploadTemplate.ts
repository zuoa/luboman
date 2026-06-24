import { request } from '@umijs/max';
import { REQUEST_HOST } from '@/constants';

/** B 站投稿模板列表（后端返回全量） */
export async function listBiliUploadTemplate(options?: { [key: string]: any }) {
  return request<API.BiliUploadTemplateInfo[]>(
    REQUEST_HOST + '/v1/BiliUploadTemplate/listAll',
    {
      method: 'POST',
      data: {},
      ...(options || {}),
    },
  );
}

/** 新建投稿模板（后端会自动补充"录播Man"标签） */
export async function addBiliUploadTemplate(
  body: API.BiliUploadTemplateInfo,
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/BiliUploadTemplate/add', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}

/** 更新投稿模板 */
export async function updateBiliUploadTemplate(
  body: Partial<API.BiliUploadTemplateInfo> & { id: number },
  options?: { [key: string]: any },
) {
  return request<API.BiliUploadTemplateInfo>(
    REQUEST_HOST + '/v1/BiliUploadTemplate/update',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/** 删除投稿模板 */
export async function delBiliUploadTemplate(
  id: number,
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/BiliUploadTemplate/del', {
    method: 'POST',
    data: { id },
    ...(options || {}),
  });
}
