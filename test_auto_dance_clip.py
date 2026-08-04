"""自动舞蹈切片跨分段边界拼接逻辑的离线验证（mock ffmpeg 与 DB）。"""
import contextlib
import datetime
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import luboman.core.dance_clip as dc
from luboman.database import models as models_mod


class DummyCM:
    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeRecord:
    def __init__(self, rid, room_id, video, begin, end):
        self.id = rid
        self.live_room_id = room_id
        self.video = video
        self.begin_time = begin
        self.end_time = end


class FakeRoom:
    room_name = '测试房间'


def make_task(task_id, source, source_ids):
    return {
        'task_id': task_id,
        'source': source,
        'source_record_file_ids': source_ids,
        'params': {},
        'status': 'RUNNING',
    }


def run():
    tmp = tempfile.mkdtemp()
    os.environ.setdefault('LUBOMAN_TEST', '1')

    begin1 = datetime.datetime(2026, 7, 29, 20, 0, 0)
    end1 = begin1 + datetime.timedelta(seconds=600)
    begin2 = end1 + datetime.timedelta(seconds=3)  # 间隔 3s < 10s
    end2 = begin2 + datetime.timedelta(seconds=600)

    records = {
        1: FakeRecord(1, 42, os.path.join(tmp, 'seg1.flv'), begin1, end1),
        2: FakeRecord(2, 42, os.path.join(tmp, 'seg2.flv'), begin2, end2),
    }
    for r in records.values():
        open(r.video, 'wb').write(b'fake')

    tasks = {
        't1': make_task('t1', 'AUTO', [1]),
        't2': make_task('t2', 'AUTO', [2]),
        't3': make_task('t3', 'MANUAL', [1]),
    }

    registered = []  # upsert_clip_record_file payloads
    cuts = []        # (src, dst, start, end, accurate)
    concats = []     # (pieces, dst)

    def fake_detect(src, params, abort_event=None):
        # seg1: 舞蹈从 300s 持续到文件尾（600s）；seg2: 开头到 200s 仍是舞蹈
        # 返回值含各区间聚合的三分屏线位（供抖音竖屏精剪）
        if src.endswith('seg1.flv'):
            return [(300.0, 600.0)], 600.0, [(0.33, 0.66)]
        return [(0.0, 200.0)], 600.0, [(0.34, 0.67)]

    def fake_cut(src, dst, start, end, accurate=False):
        cuts.append((src, dst, start, end, accurate))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, 'wb').write(b'piece')

    def fake_concat(pieces, dst):
        concats.append((list(pieces), dst))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, 'wb').write(b'merged')

    def fake_upsert(payload):
        registered.append(payload)
        return {'id': 100 + len(registered), 'video': payload['video']}, True

    finished = {}

    with mock.patch.object(dc, 'detect_three_split_intervals', side_effect=fake_detect), \
         mock.patch.object(dc, 'cut_clip', side_effect=fake_cut), \
         mock.patch.object(dc, 'concat_clips', side_effect=fake_concat), \
         mock.patch.object(dc, 'get_video_dir', return_value=tmp), \
         mock.patch('luboman.database.db.DB') as DB, \
         mock.patch.object(models_mod, 'RecordFile') as RF, \
         mock.patch.object(models_mod, 'LiveRoom') as LR, \
         mock.patch.object(models_mod, 'db') as dbmock:
        dbmock.connection_context = DummyCM()
        RF.get_by_id.side_effect = lambda rid: records[rid]
        LR.get_or_none.return_value = FakeRoom()
        DB.get_clip_task.side_effect = lambda task_id=None, row_id=None: tasks[task_id]
        DB.upsert_clip_record_file.side_effect = fake_upsert
        DB.finish_clip_task.side_effect = lambda tid, ok, **kw: finished.setdefault(
            tid, (ok, kw.get('clip_record_file_ids')))

        # 分段1完成：尾部区间应挂起，不产生切片
        dc.run_clip_task('t1')
        assert finished['t1'][0] is True
        assert finished['t1'][1] == [], f"seg1 不应产出切片: {finished['t1'][1]}"
        assert not registered, 'seg1 不应注册切片'
        assert dc._PENDING_TAILS.get(42), 'seg1 尾部应挂起'
        print('PASS 1: seg1 尾部区间挂起，未提前切片')

        # 分段2完成：开头区间与挂起尾部拼接为一个切片
        dc.run_clip_task('t2')
        assert len(concats) == 1, f'应发生一次拼接: {concats}'
        assert len(registered) == 1, f'应注册一个合并切片: {registered}'
        merged = registered[0]
        assert merged['duration_seconds'] == 300 + 200, merged
        assert merged['begin_time'] == begin1 + datetime.timedelta(seconds=300), merged['begin_time']
        assert merged['end_time'] == begin2 + datetime.timedelta(seconds=200), merged['end_time']
        assert merged['series_code'] == 'CLIP:1', merged['series_code']
        # 挂起态携带的线位应透传到合并切片的 upload_info（供抖音竖屏精剪）
        assert merged.get('upload_info', {}).get('three_split_lines') == [0.33, 0.66], merged.get('upload_info')
        assert 42 not in dc._PENDING_TAILS, '拼接后挂起应清空'
        assert finished['t2'][1] == [101], finished['t2']
        print('PASS 2: seg1尾+seg2头 拼接为一个切片并注册')

        # 手动任务：行为不变，尾部不挂起
        dc.run_clip_task('t3')
        assert len(registered) == 2, '手动任务应直接切出尾部区间'
        assert 42 not in dc._PENDING_TAILS, '手动任务不应挂起'
        print('PASS 3: 手动探测行为不变（不挂起、不拼接）')

    # 冲刷：再造一个挂起，然后 drain
    pend = dc._make_raw_pending(
        {'id': 9, 'live_room_id': 43, 'video': records[1].video,
         'begin_time': begin1, 'end_time': end1, 'room_name': 'x'},
        'x', 100.0, 600.0, dc.load_detect_params({}),
    )
    dc._hold_pending_tail(43, pend)
    with mock.patch.object(dc, 'cut_clip', side_effect=fake_cut), \
         mock.patch.object(dc, 'get_video_dir', return_value=tmp), \
         mock.patch('luboman.database.db.DB') as DB2:
        DB2.upsert_clip_record_file.side_effect = fake_upsert
        cid = dc.drain_pending_tail(43)
        assert cid == 103, cid
        assert registered[-1]['duration_seconds'] == 500
        assert 43 not in dc._PENDING_TAILS
        print('PASS 4: 直播结束冲刷挂起片段为普通切片')

    print('全部边界拼接用例通过')


if __name__ == '__main__':
    run()
