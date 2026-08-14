#!/usr/bin/env bash
# ============================================================
# Business Agent 一键启动：LLM 通道（workbuddy2api）+ 后端
# ============================================================
# 解决"启动项目要全部启动"：自动读取 .env 的 BIZ_LLM_PROVIDER /
# BIZ_CODEBUDDY_BASE_URL，若走 codebuddy 通道则先拉起对应端口实例，
# 再以管道方式启动后端（8000）。
#
# 用法（Git Bash）:
#   ./start-all.sh            # 前台：LLM 通道(若需) + 后端
#   ./start-all.sh &          # 后台运行（后端日志 logs/server_uvicorn.log）
#   ./start-all.sh status     # 查看各服务状态
#   ./start-all.sh stop       # 停止后端（LLM 通道由 start-wb2api-*.sh stop 管理）
# ============================================================
set -u
cd "$(dirname "$0")"

WB2API_DIR="D:/workbuddy2api"
PORT_8788="8788"
PORT_8787="8787"

# ---------- 读取 .env ----------
LLM_PROVIDER=$(grep -E "^BIZ_LLM_PROVIDER=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' \r')
CODEBUDDY_URL=$(grep -E "^BIZ_CODEBUDDY_BASE_URL=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' \r')
[ -z "$LLM_PROVIDER" ] && LLM_PROVIDER="deepseek"
echo "[1/3] BIZ_LLM_PROVIDER=$LLM_PROVIDER  BIZ_CODEBUDDY_BASE_URL=${CODEBUDDY_URL:-（未配置）}"

# ---------- 1. 启动 LLM 通道（codebuddy 时） ----------
ensure_wb2api() {
  local port="$1" script="$2"
  if curl -s -m 3 "http://127.0.0.1:${port}/health" 2>/dev/null | grep -q '"status":"ok"'; then
    echo "  [OK] workbuddy2api 已在运行 :${port}"
  else
    echo "  启动 workbuddy2api :${port}（$script）…"
    (cd "$WB2API_DIR" && ./"$script" start)
    for _ in 1 2 3 4 5 6; do
      curl -s -m 3 "http://127.0.0.1:${port}/health" 2>/dev/null | grep -q '"status":"ok"' && break
      sleep 2
    done
    if curl -s -m 3 "http://127.0.0.1:${port}/health" 2>/dev/null | grep -q '"status":"ok"'; then
      echo "  [OK] workbuddy2api :${port} 就绪"
    else
      echo "  [!!] workbuddy2api :${port} 启动失败，请查看 $WB2API_DIR/converter_remote.log（或 converter.log）"
    fi
  fi
}

if [ "$LLM_PROVIDER" = "codebuddy" ]; then
  case "$CODEBUDDY_URL" in
    *":8788"*) ensure_wb2api "$PORT_8788" "start-wb2api-remote.sh" ;;
    *":8787"*) ensure_wb2api "$PORT_8787" "start-wb2api.sh" ;;
    *) echo "  [!!] 无法从 BIZ_CODEBUDDY_BASE_URL 识别端口（$CODEBUDDY_URL），跳过 LLM 通道启动" ;;
  esac
else
  echo "  provider=$LLM_PROVIDER 直连，无需本地 LLM 通道"
fi

# ---------- 2. 后端状态/停止 ----------
case "${1:-}" in
  status)
    echo "[2/3] 后端状态："
    curl -s -m 3 http://127.0.0.1:8000/health 2>/dev/null | head -c 200 || echo "  后端未运行"
    echo
    exit 0
    ;;
  stop)
    PID=$(netstat -ano 2>/dev/null | grep ":8000" | grep LISTEN | awk '{print $5}' | sort -u | head -1)
    if [ -n "$PID" ]; then taskkill -F -PID "$PID" 2>/dev/null && echo "[OK] 后端已停止 (PID $PID)"; else echo "[-] 后端未在运行"; fi
    exit 0
    ;;
esac

# ---------- 3. 启动后端（管道方式，Windows 子进程兼容） ----------
if curl -s -m 3 http://127.0.0.1:8000/health 2>/dev/null | grep -q '"status":"ok"'; then
  echo "[3/3] 后端已在运行 :8000"
else
  echo "[3/3] 启动后端 :8000（管道方式）…"
  mkdir -p logs
  PYTHON="C:/Users/z'z/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
  exec "$PYTHON" -m uvicorn main:app --host 127.0.0.1 --port 8000 2>&1 | tee -a logs/server_uvicorn.log
fi
