import os


def bili_uploader(title, description, file_list, tid, tags=['ajrec'], cover=None):
    from ajrec.core.upload import BiliBili, Data
    video = Data()
    video.title = title
    video.desc = description
    # 设置视频分区,默认为122 野生技能协会
    video.tid = tid
    video.set_tag(tags)
    lines = 'AUTO'
    tasks = 3
    dtime = 7200  # 延后时间，单位秒
    with BiliBili(video) as bili:
        bili.login("bili.cookie", {})
        # bili.login_by_password("username", "password")
        for file in file_list:
            video_part = bili.upload_file(file, lines=lines, tasks=tasks)  # 上传视频，默认线路AUTO自动选择，线程数量3。
            video.append(video_part)  # 添加已经上传的视频
        video.delay_time(dtime)  # 设置延后发布（2小时~15天）
        if cover and os.path.exists(cover):
            video.cover = bili.cover_up(cover)
        ret = bili.submit()  # 提交视频
