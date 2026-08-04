#!/usr/bin/env python3
"""抖音创作者平台扫码登录，生成 storage_state cookie（人工在本机运行一次）。

用法:
    python scripts/douyin_cookie_gen.py [--output 路径] [--label 账号名]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luboman.core.douyin_login import main

if __name__ == '__main__':
    raise SystemExit(main())
