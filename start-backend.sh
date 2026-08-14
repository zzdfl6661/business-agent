#!/usr/bin/env bash
# ============================================================
# Business Agent 后端启动脚本
# ============================================================
# ⚠️ 必须用「管道」方式启动（| tee）：uvicorn 的 stdout 如果被直接重定向到文件
#    （nohup > file 或 Popen stdout=file），Windows 句柄继承会让其 subprocess 子进程
#    （python/playwright）启动即失败（rc=1 零输出）→ 数据采集登录态注入必挂。
#    管道方式（tee）同时满足：后台运行 + 日志落盘 + 子进程正常。
#
# 用法（Git Bash）:
#   ./start-backend.sh          # 前台运行（终端挂着，Ctrl+C 停止）
#   ./start-backend.sh &        # 后台运行（日志在 logs/server_uvicorn.log）
# ============================================================
cd "$(dirname "$0")" || exit 1
mkdir -p logs

PYTHON="C:/Users/z'z/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

exec "$PYTHON" -m uvicorn main:app --host 127.0.0.1 --port 8000 2>&1 | tee -a logs/server_uvicorn.log
