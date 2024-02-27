import logging
import os

from ajrec.core.upload import BiliBili, Data

from ajrec.core.decorators import PluginTool
from ajrec.core.upload import Uploader

logger = logging.getLogger('ajrec')


@PluginTool.upload(platform="biliweb")
class BiliWebUploader(Uploader):
    def __init__(self, file_list, **kwargs):
        super().__init__(file_list)
        self.title = kwargs.get('title')
        self.description = kwargs.get('description')
        self.tid = kwargs.get('tid')
        self.tags = kwargs.get('tags', ['ajrec'])
        self.cover = kwargs.get('cover')
        self.bili_cookie = kwargs.get('bili_cookie', 'bili.cookie')

    def upload(self):
        video = Data()
        video.title = self.title
        video.desc = self.description
        # 设置视频分区,默认为122 野生技能协会
        video.tid = self.tid
        video.set_tag(self.tags)
        lines = 'AUTO'
        tasks = 3
        dtime = 7200  # 延后时间，单位秒
        with BiliBili(video) as bili:
            bili.login(self.bili_cookie, {})
            # bili.login_by_password("username", "password")
            for file_info in self.file_list:
                video_part = bili.upload_file(file_info['video'], lines=lines, tasks=tasks)  # 上传视频，默认线路AUTO自动选择，线程数量3。
                video.append(video_part)  # 添加已经上传的视频
            video.delay_time(dtime)  # 设置延后发布（2小时~15天）
            if self.cover and os.path.exists(self.cover):
                video.cover = bili.cover_up(self.cover)
            ret = bili.submit()  # 提交视频
