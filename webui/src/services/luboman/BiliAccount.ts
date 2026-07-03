import { REQUEST_HOST } from '@/constants';
import { request } from '@umijs/max';

/** B 站账号列表（后端返回全量） */
export async function listBiliAccount(options?: { [key: string]: any }) {
  return request<API.BiliAccountInfo[]>(
    REQUEST_HOST + '/v1/BiliAccount/listAll',
    {
      method: 'POST',
      data: {},
      ...(options || {}),
    },
  );
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

/** 更新 B 站账号；用于重新登录后刷新 cookies。 */
export async function updateBiliAccount(
  body: Partial<API.BiliAccountInfo> & { id: number },
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/BiliAccount/update', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}

/** 检测 B 站账号登录态；不传 id 时检测全部账号。 */
export async function checkBiliAccountLogin(
  body?: { id?: number },
  options?: { [key: string]: any },
) {
  return request<API.BiliAccountLoginCheckSummary>(
    REQUEST_HOST + '/v1/BiliAccount/loginCheck',
    {
      method: 'POST',
      data: body || {},
      ...(options || {}),
    },
  );
}

/** 启动 biliup-rs 登录会话。 */
export async function startBiliupLogin(
  body: { bili_cookies_filepath?: string; account_name?: string },
  options?: { [key: string]: any },
) {
  return request<API.BiliupLoginSession>(
    REQUEST_HOST + '/v1/BiliAccount/biliupLogin/start',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/** 获取 biliup-rs 登录会话状态。 */
export async function getBiliupLoginStatus(
  body: { session_id: string; since?: number },
  options?: { [key: string]: any },
) {
  return request<API.BiliupLoginSession>(
    REQUEST_HOST + '/v1/BiliAccount/biliupLogin/status',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/** 停止 biliup-rs 登录会话。 */
export async function stopBiliupLogin(
  body: { session_id: string },
  options?: { [key: string]: any },
) {
  return request<API.BiliupLoginSession>(
    REQUEST_HOST + '/v1/BiliAccount/biliupLogin/stop',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
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
