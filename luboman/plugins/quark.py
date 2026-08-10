import logging
import os

from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.quark_client import QuarkAuthError, QuarkApiError, QuarkClient
from luboman.core.upload import Uploader
from luboman.core.utils import get_data_dir

logger = logging.getLogger('luboman')


def _remote_dir_of(local_path, root_prefix):
    """本地路径映射为夸克网盘目录，复用本地目录结构。

    {data_dir}/video/{platform}/{room}/{day}/xx.mp4 -> video/{platform}/{room}/{day}
    不在 data 目录下的路径退化为末两级目录。
    """
    local_dir = os.path.dirname(os.path.abspath(local_path))
    rel = os.path.relpath(local_dir, get_data_dir())
    if rel.startswith('..'):
        parts = [p for p in local_dir.split(os.sep) if p]
        rel = '/'.join(parts[-2:])
    rel = rel.replace(os.sep, '/')
    return f'{root_prefix}/{rel}' if root_prefix else rel


@PluginTool.upload(platform="quark")
class Quark(Uploader):
    def __init__(self, file_list):
        super().__init__(file_list)

    def upload(self):
        cookie = (config.get('quark_cookie') or '').strip()
        if not cookie:
            return {'success': False,
                    'error_message': '未配置夸克网盘 Cookie，请在系统配置页填写 quark_cookie'}

        client = QuarkClient(cookie)
        try:
            client.check_cookie()
        except QuarkAuthError as e:
            return {'success': False,
                    'error_message': f'夸克网盘 Cookie 已失效，请重新获取并更新配置: {e}'}
        except QuarkApiError as e:
            return {'success': False, 'error_message': f'夸克网盘连接失败: {e}'}

        root_prefix = (config.get('quark_root_dir') or '').strip('/')
        uploaded, failed = [], []
        for file_info in self.file_list:
            path = file_info.get('video')
            if not path or not os.path.exists(path):
                continue
            try:
                remote_dir = _remote_dir_of(path, root_prefix)
                pdir_fid = client.ensure_dir(remote_dir)
                unique_name = client.find_unique_name(pdir_fid, os.path.basename(path))
                logger.info(f'正在上传 {path} 到夸克网盘 /{remote_dir}/{unique_name}')
                client.upload_file(path, pdir_fid, file_name=unique_name)
                uploaded.append(path)
            except QuarkAuthError as e:
                # 上传中途 cookie 失效：整批没有意义继续，直接失败返回
                failed.append(path)
                logger.error(f'夸克网盘 Cookie 上传中途失效: {e}')
                if client.cookie_changed:
                    _persist_cookie(client.cookie)
                return {'success': False,
                        'error_message': f'夸克网盘 Cookie 上传中途失效，请重新获取: {e}',
                        'uploaded': uploaded, 'failed': failed}
            except (QuarkApiError, OSError) as e:
                failed.append(path)
                logger.exception(f'夸克上传失败 {path}: {e}')

        if client.cookie_changed:
            _persist_cookie(client.cookie)

        if failed and not uploaded:
            return {'success': False, 'error_message': '全部文件上传失败', 'failed': failed}
        return {'success': True, 'uploaded': uploaded, 'failed': failed}


def _persist_cookie(cookie):
    """把续期后的 cookie 回写到全局配置。"""
    try:
        config.set_persistent('quark_cookie', cookie)
        logger.info('夸克网盘 Cookie 已自动续期并回写配置')
    except Exception:
        logger.exception('回写夸克网盘 Cookie 失败')
