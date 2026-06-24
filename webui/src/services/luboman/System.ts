import { request } from '@umijs/max';
import { REQUEST_HOST } from '@/constants';

/** 健康检查，后端返回纯文本 'pong' */
export async function ping(options?: { [key: string]: any }) {
  return request<string>(REQUEST_HOST + '/ping', {
    method: 'GET',
    ...(options || {}),
  });
}

/**
 * 获取 B 站投稿分区列表（archive/pre）。
 * 后端用第一个激活账号的 cookies 调用 B 站接口，返回 B 站原始响应：
 * { code, message, data: { type_list: [{ tid, typename }] } }。
 * 无激活账号时后端返回 { success:false } → 会被统一错误层抛错，调用方 try/catch 降级。
 */
export async function getBiliArchivePre(options?: { [key: string]: any }) {
  return request<API.BiliArchivePre>(REQUEST_HOST + '/bili/archive/pre', {
    method: 'POST',
    data: {},
    ...(options || {}),
  });
}
