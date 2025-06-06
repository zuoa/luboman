import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


class VideoMerger:
    def __init__(self, video_files, output_dir="merged_videos", time_gap_minutes=3):
        """
        初始化视频合并器

        Args:
            video_files: 视频文件路径列表，可以是字符串列表或Path对象列表
            output_dir: 输出目录路径
            time_gap_minutes: 时间间隔阈值（分钟）
        """
        self.video_files = [Path(f) for f in video_files]
        self.output_dir = Path(output_dir)
        self.time_gap_minutes = time_gap_minutes
        self.output_dir.mkdir(exist_ok=True)

    def extract_timestamp_from_filename(self, filename):
        """
        从文件名中提取时间戳，支持多种常见格式：
        - 牛小蘑菇.2025_06_04_00_01_14.flv (主要格式)
        - 20240101_143052 (年月日_时分秒)
        - 2024-01-01_14-30-52
        - 20240101143052
        - video_20240101_143052.mp4
        等格式
        """
        patterns = [
            # 主要格式：牛小蘑菇.2025_06_04_00_01_14.flv
            r'\.(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})\.',
        ]

        for pattern in patterns:
            match = re.search(pattern, filename)
            if match:
                year, month, day, hour, minute, second = map(int, match.groups())
                try:
                    return datetime(year, month, day, hour, minute, second)
                except ValueError:
                    continue

        return None

    def get_video_files_with_timestamps(self):
        """获取所有视频文件及其时间戳"""
        video_files = []

        for file_path in self.video_files:
            if file_path.is_file() and file_path.exists():
                timestamp = self.extract_timestamp_from_filename(file_path.name)
                if timestamp:
                    video_files.append((file_path, timestamp))
                else:
                    print(f"警告: 无法从文件名提取时间戳: {file_path.name}")
            else:
                print(f"警告: 文件不存在或不是文件: {file_path}")

        # 按时间戳排序
        video_files.sort(key=lambda x: x[1])
        return video_files

    def group_videos_by_time_gap(self, video_files):
        """根据时间间隔将视频文件分组"""
        if not video_files:
            return []

        groups = []
        current_group = [video_files[0]]

        for i in range(1, len(video_files)):
            current_file, current_time = video_files[i]
            prev_file, prev_time = video_files[i - 1]

            time_diff = (current_time - prev_time).total_seconds() / 60  # 转换为分钟

            if time_diff <= self.time_gap_minutes:
                current_group.append(video_files[i])
            else:
                groups.append(current_group)
                current_group = [video_files[i]]

        groups.append(current_group)
        return groups

    def create_file_list(self, video_group, temp_dir):
        """为ffmpeg创建文件列表"""
        file_list_path = temp_dir / "file_list.txt"
        with open(file_list_path, 'w', encoding='utf-8') as f:
            for file_path, _ in video_group:
                # 使用相对路径或绝对路径，确保路径中的特殊字符被正确处理
                f.write(f"file '{file_path.absolute()}'\n")
        return file_list_path

    def merge_videos_with_ffmpeg(self, video_group, output_filename):
        """使用ffmpeg合并视频组"""
        temp_dir = Path("temp_merge")
        temp_dir.mkdir(exist_ok=True)

        try:
            # 创建文件列表
            file_list_path = self.create_file_list(video_group, temp_dir)
            output_path = self.output_dir / output_filename

            # 构建ffmpeg命令
            cmd = [
                'ffmpeg',
                '-f', 'concat',
                '-safe', '0',
                '-i', str(file_list_path),
                '-c', 'copy',  # 直接复制流，不重新编码（更快）
                '-y',  # 覆盖输出文件
                str(output_path)
            ]

            print(f"正在合并 {len(video_group)} 个文件到 {output_filename}")
            print(f"文件列表: {[f.name for f, _ in video_group]}")

            # 执行ffmpeg命令
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                print(f"✓ 成功合并到: {output_path}")
                return True
            else:
                print(f"✗ 合并失败: {result.stderr}")
                return False

        except Exception as e:
            print(f"✗ 合并过程中出错: {e}")
            return False
        finally:
            # 清理临时文件
            if temp_dir.exists():
                for temp_file in temp_dir.iterdir():
                    temp_file.unlink()
                temp_dir.rmdir()

    def process_all_videos(self):
        """处理所有视频文件"""
        print(f"正在处理 {len(self.video_files)} 个视频文件")
        video_files = self.get_video_files_with_timestamps()

        if not video_files:
            print("未找到包含有效时间戳的视频文件")
            return

        print(f"成功解析 {len(video_files)} 个视频文件的时间戳")

        # 按时间间隔分组
        groups = self.group_videos_by_time_gap(video_files)
        print(f"根据 {self.time_gap_minutes} 分钟间隔，分为 {len(groups)} 组")

        # 合并每组视频
        success_count = 0
        for i, group in enumerate(groups, 1):
            # 生成输出文件名，使用第一个文件的时间戳
            first_timestamp = group[0][1]
            last_timestamp = group[-1][1]

            # 提取原始文件名的前缀（如"牛小蘑菇"）
            first_filename = group[0][0].name
            prefix_match = re.match(r'^([^.]+)\.', first_filename)
            prefix = prefix_match.group(1) if prefix_match else "merged"

            output_filename = f"{prefix}_{first_timestamp.strftime('%Y_%m_%d_%H_%M_%S')}"
            if len(group) > 1:
                output_filename += f"_to_{last_timestamp.strftime('%H_%M_%S')}"
            output_filename += ".mp4"

            print(f"\n--- 处理第 {i} 组 ---")
            if self.merge_videos_with_ffmpeg(group, output_filename):
                success_count += 1

        print(f"\n=== 处理完成 ===")
        print(f"成功合并: {success_count}/{len(groups)} 组")
        print(f"输出目录: {self.output_dir.absolute()}")


def main():
    # 配置参数
    input_directory = input("请输入视频文件所在目录路径: ").strip()
    if not input_directory:
        input_directory = "."  # 当前目录

    # 检查输入目录是否存在
    if not os.path.exists(input_directory):
        print(f"错误: 输入目录不存在: {input_directory}")
        return

    # 获取目录中的所有视频文件
    input_dir = Path(input_directory)
    video_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.m4v'}
    video_files = []

    for file_path in input_dir.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in video_extensions:
            video_files.append(file_path)

    if not video_files:
        print(f"在目录 {input_directory} 中未找到视频文件")
        return

    print(f"找到 {len(video_files)} 个视频文件")

    output_directory = input("请输入输出目录路径（回车使用默认 'merged_videos'）: ").strip()
    if not output_directory:
        output_directory = "merged_videos"

    time_gap = input("请输入时间间隔阈值（分钟，回车使用默认 3 分钟）: ").strip()
    try:
        time_gap = int(time_gap) if time_gap else 3
    except ValueError:
        time_gap = 3
        print("时间间隔输入无效，使用默认值 3 分钟")

    # 检查ffmpeg是否可用
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("错误: 未找到ffmpeg，请确保已安装ffmpeg并添加到系统PATH")
        return

    # 创建合并器并处理视频
    merger = VideoMerger(video_files, output_directory, time_gap)
    merger.process_all_videos()


# 也可以直接使用文件列表的方式
def merge_video_files(file_list, output_dir="merged_videos", time_gap_minutes=3):
    """
    直接使用文件列表进行视频合并的便捷函数

    Args:
        file_list: 视频文件路径列表
        output_dir: 输出目录
        time_gap_minutes: 时间间隔阈值（分钟）

    Example:
        files = [
            "牛小蘑菇.2025_06_04_00_01_14.flv",
            "牛小蘑菇.2025_06_04_00_03_15.flv",
            "牛小蘑菇.2025_06_04_00_08_20.flv"
        ]
        merge_video_files(files, "output", 3)
    """
    merger = VideoMerger(file_list, output_dir, time_gap_minutes)
    merger.process_all_videos()


if __name__ == "__main__":
    main()