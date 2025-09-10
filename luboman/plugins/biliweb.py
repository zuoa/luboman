import logging
import os

from luboman.core.decorators import PluginTool
from luboman.core.upload import BiliBili, Data
from luboman.core.upload import Uploader
from luboman.core.utils import format_live_prop_text

logger = logging.getLogger('luboman')


@PluginTool.upload(platform="biliweb")
class BiliWebUploader(Uploader):
    def __init__(self, file_list, room_data):
        super().__init__(file_list)
        self.room_data = room_data

    def upload(self):
        template_info = self.room_data.get('bili_upload_template')
        if not template_info:
            logger.warning(f"未设置上传模板")
            return

        bili_account = template_info.get('bili_account')
        if not bili_account:
            logger.warning(f"未设置bilibili账号")
            return

        template_title = template_info.get('title', '【{room_name}】{room_title} %Y年%m月%d日 %H时')
        template_description = template_info.get('description',
                                                 '【{room_name}】直播间地址：{room_url} \n如有侵权请联系我删除\n---\n接主播直播录制，可投稿B站/网盘，v:jiadano')
        video = Data()
        video.title = format_live_prop_text(template_title, self.room_data)
        video.desc = format_live_prop_text(template_description, self.room_data)
        # 设置视频分区,默认为122 野生技能协会
        video.tid = template_info.get('tid', 171)
        video.copyright = template_info.get('copy_right', 1)
        if template_info.get('copy_right', 1) == 2:
            video.source = self.room_data["room_url"]
        tags = template_info.get('tags')
        if not tags:
            tags = ['录播Man']
        video.set_tag(tags)

        if self.room_data.get('bili_upower_level_id'):
            video.upower_level_id = self.room_data.get('bili_upower_level_id')
            video.charging_pay = 1

        lines = template_info.get('lines', 'AUTO')
        tasks = template_info.get('threads', 5)
        with BiliBili(video) as bili:
            cookies_file = bili_account.get('bili_cookies_filepath')
            if os.path.exists(cookies_file):
                cookies_file_content = open(cookies_file, 'r').read()

            if cookies_file_content:
                bili.login(cookies_file)
            else:
                bili.login_by_cookies(bili_account.get('bili_cookies'))

            for file_info in self.file_list:
                if not os.path.exists(file_info['video']):
                    continue
                video_part = bili.upload_file(file_info['video'], lines=lines, tasks=tasks)  # 上传视频，默认线路AUTO自动选择，线程数量3。
                video_part["title"] = os.path.splitext(os.path.basename(file_info['video']))[0][:80]
                video.append(video_part)  # 添加已经上传的视频
            if template_info.get('dtime'):
                video.delay_time(template_info.get('dtime'))  # 设置延后发布（2小时~15天）
            if template_info.get('cover_path') and os.path.exists(template_info.get('cover_path')):
                video.cover = bili.cover_up(template_info.get('cover_path'))
            ret = bili.submit()  # 提交视频


if __name__ == '__main__':
    video = Data()
    video.title = "测试标题"
    video.desc = "测试描述"
    video.tid = 122  # 分区ID
    video.copyright = 1
    video.charging_pay = 1
    video.upower_level_id = '952390697301177415'
    video.set_tag(['测试标签'])
    with BiliBili(video) as bili:
        cookies_file = "/Users/yujian/Downloads/biliupR-v0.1.19-x86_64-macos/cookies.json"  # 替换为实际的cookies文件路径
        if os.path.exists(cookies_file):
            cookies_file_content = open(cookies_file, 'r').read()
            if cookies_file_content:
                bili.login(cookies_file)
            else:
                bili.login_by_cookies("your_bili_cookies_here")  # 替换为实际的cookies内容

        video_part = bili.upload_file("/Users/yujian/Downloads/qx6kLOhg3gYDJVhn.mp4")  # 替换为实际的视频文件路径
        video.append(video_part)
        ret = bili.submit()
        print(ret)