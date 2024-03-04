import logging
import os

from luboman.core.upload import BiliBili, Data

from luboman.core.decorators import PluginTool
from luboman.core.upload import Uploader
from luboman.core.utils import format_live_prop_text
from luboman.database.models import BiliUploadTemplate, BiliAccount

logger = logging.getLogger('luboman')


@PluginTool.upload(platform="biliweb")
class BiliWebUploader(Uploader):
    def __init__(self, file_list, room_data):
        super().__init__(file_list)
        self.room_data = room_data
        # TODO: 解耦合，插件内部不和数据库交互

    def upload(self):
        bili_upload_template_id = self.room_data.get('bili_upload_template_id')
        if bili_upload_template_id is None:
            logger.error(f"bili_upload_template_id is None")
            return

        template_info = BiliUploadTemplate.get_by_id_(bili_upload_template_id)
        if not template_info:
            logger.error(f"bili_upload_template_id: {bili_upload_template_id} not found")
            return

        if template_info.bili_account_id is None:
            logger.error(f"bili_account_id is None")
            return

        bili_account = BiliAccount.get_by_id(template_info.bili_account_id)
        if not bili_account:
            logger.error(f"bili_account_id: {template_info.bili_account_id} not found")
            return

        video = Data()
        video.title = format_live_prop_text(template_info.title, self.room_data)
        video.desc = format_live_prop_text(template_info.description, self.room_data)
        # 设置视频分区,默认为122 野生技能协会
        video.tid = template_info.tid
        video.copyright = template_info.copy_right
        if template_info.copy_right == 2:
            video.source = self.room_data["room_url"]
        tags = template_info.tags
        if not tags:
            tags = ['录播Man']
        video.set_tag(tags)
        lines = template_info.lines
        tasks = template_info.threads
        with BiliBili(video) as bili:
            bili.login(bili_account.bili_cookies_filepath, {})
            # bili.login_by_password("username", "password")
            for file_info in self.file_list:
                video_part = bili.upload_file(file_info['video'], lines=lines, tasks=tasks)  # 上传视频，默认线路AUTO自动选择，线程数量3。
                video.append(video_part)  # 添加已经上传的视频
            if template_info.dtime:
                video.delay_time(template_info.dtime)  # 设置延后发布（2小时~15天）
            if template_info.cover_path and os.path.exists(template_info.cover_path):
                video.cover = bili.cover_up(template_info.cover_path)
            ret = bili.submit()  # 提交视频
