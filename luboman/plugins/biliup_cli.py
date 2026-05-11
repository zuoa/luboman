import json
import logging
import os
import shlex
import subprocess
from typing import Any, Dict, Iterable, List, Optional

from luboman.core.decorators import PluginTool
from luboman.core.upload import Uploader
from luboman.core.utils import format_live_prop_text

logger = logging.getLogger('luboman')


def _config_get(key: str, default=None):
    from luboman.config import config

    return config.get(key, default)


def _first_present(mapping: Dict[str, Any], *keys: str, default=None):
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != '':
            return value
    return default


def _as_int(value, default=None):
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _tags_to_cli(tags) -> str:
    if not tags:
        return '录播Man'
    if isinstance(tags, str):
        return tags
    if isinstance(tags, Iterable):
        return ','.join(str(tag) for tag in tags if tag)
    return str(tags)


def _line_to_cli(line: Optional[str]) -> Optional[str]:
    if not line:
        return None
    line = str(line).strip()
    if not line or line.upper() == 'AUTO':
        return None
    aliases = {
        'cs-bda2': 'bda2',
        'cs-qn': 'qn',
    }
    normalized = aliases.get(line.lower(), line.lower())
    return normalized


def _submit_api_to_cli(submit_api: Optional[str]) -> Optional[str]:
    if not submit_api:
        return None
    normalized = str(submit_api).strip().lower()
    aliases = {
        'client': 'app',
        'app': 'app',
        'web': 'web',
        'b-cut-android': 'b-cut-android',
        'b_cut_android': 'b-cut-android',
    }
    return aliases.get(normalized)


def _append_option(command: List[str], option: str, value):
    if value is not None and value != '':
        command.extend([option, str(value)])


@PluginTool.upload(platform='biliup-rs')
@PluginTool.upload(platform='biliup')
@PluginTool.upload(platform='biliup_cli')
class BiliupCliUploader(Uploader):
    """Upload to Bilibili by delegating to the external biliup CLI."""

    def __init__(self, file_list, room_data):
        super().__init__(file_list)
        self.room_data = room_data

    def upload(self):
        template_info = self.room_data.get('bili_upload_template')
        if not template_info:
            logger.warning('未设置上传模板')
            return False

        bili_account = template_info.get('bili_account') or {}
        cookie_file = bili_account.get('bili_cookies_filepath')
        if not cookie_file or not os.path.exists(cookie_file):
            logger.error('biliup CLI 需要 biliup login 生成的 cookies.json，请在账号中配置有效的 bili_cookies_filepath')
            return False

        video_paths = [
            file_info['video']
            for file_info in self.file_list
            if file_info.get('video') and os.path.exists(file_info['video'])
        ]
        if not video_paths:
            logger.warning('没有可上传的视频文件')
            return False

        command = self._build_command(template_info, cookie_file, video_paths)
        logger.info('开始调用 biliup CLI 上传: %s', shlex.join(command))

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if proc.stdout:
                for line in proc.stdout:
                    logger.info('[biliup] %s', line.rstrip())
            retval = proc.wait()
        except FileNotFoundError:
            logger.error('未找到 biliup 命令，请安装 biliup 或通过 biliup_path 配置二进制路径')
            return False
        except Exception:
            logger.exception('调用 biliup CLI 上传失败')
            return False

        if retval != 0:
            logger.error('biliup CLI 上传失败，退出码: %s', retval)
            return False

        logger.info('biliup CLI 上传完成')
        return True

    def _build_command(self, template_info: Dict[str, Any], cookie_file: str, video_paths: List[str]) -> List[str]:
        biliup_path = _config_get('biliup_path', 'biliup')
        command = [biliup_path, '-u', cookie_file, 'upload']

        submit_api = _config_get('submit_api', 'app')
        submit_api_cli = _submit_api_to_cli(submit_api)
        if submit_api_cli:
            command.extend(['--submit', submit_api_cli])
        elif submit_api:
            logger.warning('不支持的 biliup submit_api: %s，已跳过 --submit 参数', submit_api)

        line = _line_to_cli(_first_present(template_info, 'lines', default=_config_get('lines')))
        if line:
            command.extend(['--line', line])

        threads = _as_int(_first_present(template_info, 'threads', default=_config_get('threads', 3)), 3)
        _append_option(command, '--limit', threads)

        title_template = template_info.get('title', '【{room_name}】{room_title} %Y年%m月%d日 %H时')
        desc_template = template_info.get(
            'description',
            '【{room_name}】直播间地址：{room_url} \n如有侵权请联系我删除\n---\n接主播直播录制，可投稿B站/网盘，v:jiadano',
        )
        title = format_live_prop_text(title_template, self.room_data)
        desc = format_live_prop_text(desc_template, self.room_data)

        copyright_value = _as_int(
            _first_present(template_info, 'copyright', 'copy_right', default=1),
            1,
        )

        _append_option(command, '--copyright', copyright_value)
        if copyright_value == 2:
            _append_option(command, '--source', _first_present(template_info, 'source', default=self.room_data.get('room_url', '')))

        _append_option(command, '--tid', _as_int(template_info.get('tid'), 171))
        _append_option(command, '--title', title)
        _append_option(command, '--desc', desc)
        _append_option(command, '--dynamic', template_info.get('dynamic'))
        _append_option(command, '--tag', _tags_to_cli(template_info.get('tags')))

        dtime = _as_int(template_info.get('dtime'))
        if dtime:
            _append_option(command, '--dtime', dtime)

        cover_path = template_info.get('cover_path')
        if cover_path:
            if os.path.exists(cover_path):
                _append_option(command, '--cover', cover_path)
            else:
                logger.warning('封面文件不存在，跳过 cover 参数: %s', cover_path)

        for field, option in (
            ('dolby', '--dolby'),
            ('hires', '--hires'),
            ('no_reprint', '--no-reprint'),
            ('open_elec', '--open-elec'),
        ):
            value = _as_int(template_info.get(field))
            if value is not None:
                _append_option(command, option, value)

        if _as_bool(template_info.get('up_selection_reply')):
            command.append('--up-selection-reply')
        if _as_bool(template_info.get('up_close_reply')):
            command.append('--up-close-reply')
        if _as_bool(template_info.get('up_close_danmu')):
            command.append('--up-close-danmu')

        extra_fields = self._build_extra_fields(template_info)
        if extra_fields:
            command.extend(['--extra-fields', json.dumps(extra_fields, ensure_ascii=False)])

        command.extend(video_paths)
        return command

    def _build_extra_fields(self, template_info: Dict[str, Any]) -> Dict[str, Any]:
        extra_fields = {}
        configured_extra = _config_get('biliup_extra_fields')
        if configured_extra:
            try:
                extra_fields.update(json.loads(configured_extra))
            except (TypeError, json.JSONDecodeError):
                logger.warning('biliup_extra_fields 不是合法 JSON，已忽略')

        template_extra = template_info.get('extra_fields')
        if isinstance(template_extra, dict):
            extra_fields.update(template_extra)

        upower_level_id = self.room_data.get('bili_upower_level_id')
        if upower_level_id:
            extra_fields.setdefault('charging_pay', 1)
            extra_fields.setdefault('upower_level_id', upower_level_id)

        return extra_fields
