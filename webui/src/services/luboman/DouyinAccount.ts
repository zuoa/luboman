import { REQUEST_HOST } from '@/constants';
import { request } from '@umijs/max';

/** 抖音账号列表（后端返回全量） */
export async function listDouyinAccount(options?: { [key: string]: any }) {
  return request<API.DouyinAccountInfo[]>(
    REQUEST_HOST + '/v1/DouyinAccount/listAll',
    {
      method: 'POST',
      data: {},
      ...(options || {}),
    },
  );
}

/**
 * 新建抖音账号。
 * body 传 douyin_cookies_filepath（扫码登录会话成功后回填）或 douyin_cookies。
 */
export async function addDouyinAccount(
  body: Partial<API.DouyinAccountInfo>,
  options?: { [key: string]: any },
) {
  return request<API.DouyinAccountInfo>(REQUEST_HOST + '/v1/DouyinAccount/add', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}

/** 更新抖音账号；用于重新登录后刷新 cookies。 */
export async function updateDouyinAccount(
  body: Partial<API.DouyinAccountInfo> & { id: number },
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/DouyinAccount/update', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}

/** 启动抖音扫码登录会话（服务端 headless 浏览器抓二维码）。 */
export async function startDouyinLogin(
  body: { douyin_cookies_filepath?: string; account_name?: string },
  options?: { [key: string]: any },
) {
  return request<API.DouyinLoginSession>(
    REQUEST_HOST + '/v1/DouyinAccount/login/start',
    {
      method: 'POST',
      data: body,
      // 服务端首次拉起浏览器 + 抓二维码较慢，放宽超时
      timeout: 90000,
      ...(options || {}),
    },
  );
}

/** 获取抖音扫码登录会话状态。 */
export async function getDouyinLoginStatus(
  body: { session_id: string; since?: number },
  options?: { [key: string]: any },
) {
  return request<API.DouyinLoginSession>(
    REQUEST_HOST + '/v1/DouyinAccount/login/status',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/** 停止抖音扫码登录会话。 */
export async function stopDouyinLogin(
  body: { session_id: string },
  options?: { [key: string]: any },
) {
  return request<API.DouyinLoginSession>(
    REQUEST_HOST + '/v1/DouyinAccount/login/stop',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/** 删除抖音账号（后端软删除，置 state_active=0） */
export async function delDouyinAccount(
  id: number,
  options?: { [key: string]: any },
) {
  return request<number>(REQUEST_HOST + '/v1/DouyinAccount/del', {
    method: 'POST',
    data: { id },
    ...(options || {}),
  });
}
