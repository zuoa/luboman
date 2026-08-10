import { REQUEST_HOST } from '@/constants';
import { request } from '@umijs/max';

/**
 * 上传文件到后端（multipart）。FormData 直接传给 data 即可，
 * 不要手动设置 Content-Type（浏览器会自动带 boundary）。
 */
async function uploadFile(path: string, file: File) {
  const formData = new FormData();
  formData.append('file', file);
  return request<API.UploadFileResult>(REQUEST_HOST + path, {
    method: 'POST',
    data: formData,
  });
}

/** 上传自定义封面图片（按直播间），落盘 {public}/cover/custom/ */
export async function uploadCover(file: File) {
  return uploadFile('/v1/Upload/cover', file);
}

/** 上传片头视频（按 B 站账号），落盘 {public}/intro/ */
export async function uploadIntro(file: File) {
  return uploadFile('/v1/Upload/intro', file);
}
