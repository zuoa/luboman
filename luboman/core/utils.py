import logging
import os
import re
import shutil
import time

logger = logging.getLogger('luboman')


def match1(text, *patterns):
    if len(patterns) == 1:
        pattern = patterns[0]
        match = re.search(pattern, text)
        if match:
            return match.group(1)
        else:
            return None
    else:
        ret = []
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                ret.append(match.group(1))
        return ret


def random_user_agent(device: str = 'desktop') -> str:
    import random
    chrome_version = random.randint(100, 120)
    if device == 'mobile':
        android_version = random.randint(9, 14)
        mobile = random.choice([
            'SM-G981B', 'SM-G9910', 'SM-S9080', 'SM-S9110', 'SM-S921B',
            'Pixel 5', 'Pixel 6', 'Pixel 7', 'Pixel 7 Pro', 'Pixel 8',
        ])
        return f'Mozilla/5.0 (Linux; Android {android_version}; {mobile}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Mobile Safari/537.36'
    return f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_version}.0.0.0 Safari/537.36'


def get_valid_filename(name):
    # s = str(name).strip().replace(" ", "_") #因为有些人会在主播名中间加入空格，为了避免和录播完毕自动改名冲突，所以注释掉
    s = re.sub(r"(?u)[^-\w.%{}\[\]【】「」\s]", "", str(name))
    if s in {"", ".", ".."}:
        raise RuntimeError("Could not derive file name from '%s'" % name)
    return s


def format_live_prop_text(formatted_str: str, room_data):
    if not formatted_str:
        formatted_str = '【{room_name}】{room_title} %Y年%m月%d日 %H时'
    prop_text = formatted_str.format(**room_data)
    prop_text = time.strftime(prop_text)
    return prop_text


def get_project_rootpath():
    """
    获取项目根目录。此函数的能力体现在，不论当前module被import到任何位置，都可以正确获取项目根目录
    :return:
    """
    path = os.path.realpath(os.curdir)
    while True:
        # PyCharm项目中，'.idea'是必然存在的，且名称唯一
        if '.idea' in os.listdir(path):
            return path
        path = os.path.dirname(path)


def get_data_dir():
    return '/data' if os.path.exists('/.dockerenv') else f'{get_project_rootpath()}/data'


def get_video_dir():
    path = os.path.join(get_data_dir(), 'video')
    os.makedirs(path, exist_ok=True)

    return path


def get_public_dir():
    path = os.path.join(get_data_dir(), 'public')
    os.makedirs(path, exist_ok=True)

    return path


def remove_filelist(file_list):
    for f in file_list:
        remove_file(f['video'])
        if f.barrage is not None:
            remove_file(f['barrage'])


def remove_dir(dir_path: str):
    try:
        shutil.rmtree(dir_path, ignore_errors=True)
        logger.info(f'删除 - {dir_path}')
    except Exception as e:
        logger.warning(f'删除失败 - {dir_path} :{e}')


def remove_file(file: str):
    try:
        os.remove(file)
        logger.info(f'删除 - {file}')
    except Exception as e:
        logger.warning(f'删除失败 - {file} :{e}')


def rename(filepath):
    try:
        os.rename(filepath + '.part', filepath)
        logger.info(f'更名 {filepath + ".part"} 为 {filepath}')
    except FileNotFoundError:
        logger.debug(f'文件不存在: {filepath + ".part"}')
    except FileExistsError:
        os.rename(filepath + '.part', filepath)
        logger.info(f'更名 {filepath + ".part"} 为 {filepath} 失败, {filepath} 已存在')


def download_file(url, local_path, headers=None):
    import requests
    if headers is None:
        headers = {}
    with requests.get(url, stream=True, headers=headers, timeout=120) as resp:
        resp.raise_for_status()
        with open(local_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):  # 增大chunk_size提高效率
                if chunk:
                    f.write(chunk)


class NamedLock:
    """
    简单实现的命名锁
    """
    from _thread import LockType
    _lock_dict = {}

    def __new__(cls, name) -> LockType:
        import threading
        if name not in cls._lock_dict:
            cls._lock_dict[name] = threading.Lock()
        return cls._lock_dict[name]
