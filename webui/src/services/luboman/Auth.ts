import { request } from '@umijs/max';
import { REQUEST_HOST } from '@/constants';

/** 密码登录。失败时后端返回 401（密码错误）/429（触发限流锁定），message 含提示 */
export async function login(password: string, options?: { [key: string]: any }) {
  return request<null>(REQUEST_HOST + '/v1/Auth/login', {
    method: 'POST',
    data: { password },
    ...(options || {}),
  });
}

/** 退出登录：清会话 cookie */
export async function logout(options?: { [key: string]: any }) {
  return request<null>(REQUEST_HOST + '/v1/Auth/logout', {
    method: 'POST',
    data: {},
    ...(options || {}),
  });
}

/**
 * 鉴权状态：{ enabled: 是否设置了访问密码, logged_in: 当前会话是否已登录 }。
 * 登录页初始化时调用，建议传 { skipErrorHandler: true } 自行降级。
 */
export async function getStatus(options?: { [key: string]: any }) {
  return request<{ enabled: boolean; logged_in: boolean }>(
    REQUEST_HOST + '/v1/Auth/status',
    {
      method: 'GET',
      ...(options || {}),
    },
  );
}
