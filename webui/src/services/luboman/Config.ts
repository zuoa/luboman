import { request } from '@umijs/max';
import { REQUEST_HOST } from '@/constants';

/** 读取全部全局配置，返回 { key: value } 字典 */
export async function getConfig(options?: { [key: string]: any }) {
  return request<Record<string, any>>(REQUEST_HOST + '/v1/Config/get', {
    method: 'POST',
    data: {},
    ...(options || {}),
  });
}

/** 写入全局配置（增量更新，未传入的键不变） */
export async function setConfig(
  body: Record<string, any>,
  options?: { [key: string]: any },
) {
  return request<null>(REQUEST_HOST + '/v1/Config/set', {
    method: 'POST',
    data: body,
    ...(options || {}),
  });
}
