import { REQUEST_HOST } from '@/constants';
import { request } from '@umijs/max';

/** 启动夸克扫码登录会话（服务端取二维码，成功后自动写回 quark_cookie）。 */
export async function startQuarkLogin(options?: { [key: string]: any }) {
  return request<API.QuarkLoginSession>(
    REQUEST_HOST + '/v1/QuarkAccount/login/start',
    {
      method: 'POST',
      data: {},
      // 服务端首次取二维码有网络开销，放宽超时
      timeout: 45000,
      ...(options || {}),
    },
  );
}

/** 获取夸克扫码登录会话状态。 */
export async function getQuarkLoginStatus(
  body: { session_id: string; since?: number },
  options?: { [key: string]: any },
) {
  return request<API.QuarkLoginSession>(
    REQUEST_HOST + '/v1/QuarkAccount/login/status',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}

/** 停止夸克扫码登录会话。 */
export async function stopQuarkLogin(
  body: { session_id: string },
  options?: { [key: string]: any },
) {
  return request<API.QuarkLoginSession>(
    REQUEST_HOST + '/v1/QuarkAccount/login/stop',
    {
      method: 'POST',
      data: body,
      ...(options || {}),
    },
  );
}
