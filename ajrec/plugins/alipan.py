import logging
import os
import subprocess
import sys

from ajrec.core.decorators import PluginTool
from ajrec.core.upload import Uploader

logger = logging.getLogger('ajrec')


@PluginTool.upload(platform="alipan")
class Alipan(Uploader):
    def __init__(self, file_list):
        super().__init__(file_list)

    def upload(self):
        for file_info in self.file_list:
            drive_dir = os.path.dirname(file_info['video'])
            command_args = ['/usr/local/bin/aliyunpan', 'upload', file_info['video'], drive_dir]

            try:
                proc = subprocess.Popen(command_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                with proc.stdout as stdout:
                    for line in iter(stdout.readline, b''):
                        decode_line = line.decode(errors='ignore')
                        logger.debug(decode_line.rstrip())
                retval = proc.wait()
            except Exception as e:
                logger.warning("command '{}' return with error : {}".format(command_args, e))
