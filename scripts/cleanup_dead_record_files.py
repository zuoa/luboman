"""一次性清理 record_file 死记录（磁盘文件已删、数据库行还在的幽灵记录）。

背景：录像列表/直播间汇总接口默认 exists_only=true，会对每条 DB 记录 stat 磁盘。
老平台历史积累的死记录太多时，每次请求要 stat 成千上万个不存在的路径，导致超时。
文件管理升级后的设计前提是"DB 行 ≈ 磁盘文件"，靠本脚本把死记录收敛掉。

用法（在老平台部署机上，用与服务相同的数据库环境变量）：
    python scripts/cleanup_dead_record_files.py            # dry-run，只统计不删除
    python scripts/cleanup_dead_record_files.py --apply    # 实际删除

安全策略：
- 只清理非 RECORDING 状态的记录（进行中的录制文件可能刚创建/尚未落盘，不动）。
- 记录有 video 路径且文件或对应 .part 存在 => 保留。
- 分批删除，避免长事务。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from luboman.database.models import db, RecordFile  # noqa: E402
from luboman.database.db import RECORD_FILE_STATUS_RECORDING  # noqa: E402

BATCH_SIZE = 500


def collect_dead_ids():
    dead_ids = []
    checked = 0
    with db.connection_context():
        # status 可能为 NULL（老数据），NULL != 'RECORDING' 在 SQL 中为 NULL 会被过滤，
        # 所以显式把 NULL 行也纳入扫描
        query = RecordFile.select(
            RecordFile.id, RecordFile.video, RecordFile.status
        ).where(
            (RecordFile.status != RECORD_FILE_STATUS_RECORDING)
            | (RecordFile.status.is_null(True))
        )
        for record in query.iterator():
            checked += 1
            path = record.video
            if path and (os.path.exists(path) or os.path.exists(f'{path}.part')):
                continue
            dead_ids.append(record.id)
            if checked % 1000 == 0:
                print(f'已检查 {checked} 行，当前死记录 {len(dead_ids)} ...', flush=True)
    return dead_ids, checked


def delete_in_batches(dead_ids):
    deleted = 0
    with db.connection_context():
        for i in range(0, len(dead_ids), BATCH_SIZE):
            chunk = dead_ids[i:i + BATCH_SIZE]
            deleted += RecordFile.delete().where(RecordFile.id.in_(chunk)).execute()
            print(f'已删除 {deleted}/{len(dead_ids)}', flush=True)
    return deleted


def main():
    parser = argparse.ArgumentParser(description='清理 record_file 死记录')
    parser.add_argument('--apply', action='store_true', help='实际删除（默认 dry-run）')
    args = parser.parse_args()

    dead_ids, checked = collect_dead_ids()
    print(f'检查完成：共 {checked} 行（不含 RECORDING），死记录 {len(dead_ids)} 行')

    if not dead_ids:
        return
    if not args.apply:
        print('dry-run 模式，未删除。确认无误后加 --apply 执行。')
        return

    deleted = delete_in_batches(dead_ids)
    print(f'清理完成，共删除 {deleted} 行死记录')


if __name__ == '__main__':
    main()
