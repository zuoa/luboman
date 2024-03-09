import logging
import os
import subprocess

from bypy import ByPy
from luboman.config import config
from luboman.core.decorators import PluginTool
from luboman.core.upload import Uploader

logger = logging.getLogger('luboman')


@PluginTool.upload(platform="bdpan")
class Baidupan(Uploader):
    def __init__(self, file_list):
        super().__init__(file_list)

    def upload(self):
        bp = ByPy(verbose=1)
        for file_info in self.file_list:
            if not os.path.exists(file_info['video']):
                continue

            drive_dir = os.path.dirname(file_info['video'])
            bp.mkdir(drive_dir)
            logger.info(f"正在上传 {file_info['video']} 到百度网盘")
            ret = bp.upload(file_info['video'], file_info['video'])
            logger.info(f"上传完成 {file_info['video']} 到百度网盘: {ret}")
