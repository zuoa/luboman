#!/bin/sh
set -e

# 完整镜像（main tag）装了 xvfb：用 xvfb-run 在虚拟显示上跑，让抖音扫码以
# headed 模式执行（headless 极易被 creator.douyin.com 风控识别，导致扫码卡在 scanned）。
# 精简镜像（main-slim，WITH_DOUYIN=false，未装 X）没有 xvfb-run，直跑 python，
# 行为与历史一致。
#
# xvfb-run -a：自动挑选空闲 display 号并设好 $DISPLAY，进程退出即清理。
# async_main.py 是纯 asyncio 应用（无 GUI 依赖），包裹安全；SIGINT/SIGTERM
# 经 exec 正常透传给 python。
if command -v xvfb-run >/dev/null 2>&1; then
  exec xvfb-run -a --server-args="-screen 0 1600x900x24" python async_main.py "$@"
else
  exec python async_main.py "$@"
fi
