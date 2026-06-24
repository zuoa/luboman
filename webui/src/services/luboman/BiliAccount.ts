import { request } from '@umijs/max';
import { REQUEST_HOST } from '@/constants';

/** B 站账号列表（后端返回全量） */
export async function listBiliAccount(options?: { [key: string]: any }) {
  return request<API.BiliAccountInfo[]>(REQUEST_HOST + '/v1/BiliAccount/listAll', {
    method: 'POST',
    data: {},
    ...(options || {}),
  });
}

/**
 * 新建 B 站账号。
 * body 至少传 bili_cookies_filepath 或 bili_cookies 二选一；
 * 后端会用 cookies 回填 account_name / account_avatar。
 */
export async function addBiliAccount(
  body: Partial<API.BiliAccountInfo>,
  options?: { [key: string]: any },
) {
  return request<API.BiliAccountInfo>(REQUEST_HOST + '/v1/BiliAccount/add', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}

/** 删除 B 站账号（后端软删除，置 state_active=0） */
export async function delBiliAccount(
  id: number,
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/BiliAccount/del', {
    method: 'POST',
    data: { id },
    ...(options || {}),
  });
}
