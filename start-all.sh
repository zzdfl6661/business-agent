# ============ 项目启动备忘（在 VSCode 终端切换到 Git Bash 后执行） ============
# 进入项目目录
#cd "/d/agent learning/Business  Agent"

# ① 一键启动全部（自动拉起 LLM 通道 8788 + 后端 8000）——推荐
# ./start-all.sh
#    想后台运行（终端可关）：
# ./start-all.sh &

# ② 查看状态 / 停止
# ./start-all.sh status                          # 看 8788 与 8000 是否就绪
# ./start-all.sh stop                            # 停后端 8000
# cd /d/workbuddy2api && ./start-wb2api-remote.sh stop   # 停 LLM 通道 8788

# ③ 分开启动（等价于一键，拆开做）：
# cd /d/workbuddy2api && ./start-wb2api-remote.sh start   # 先起 8788（10 秒内就绪）
# cd "/d/agent learning/Business  Agent" && ./start-backend.sh   # 再起后端 8000

# ④ 验证
# curl http://127.0.0.1:8788/health   # 应返回 {"status":"ok",...}
# curl http://127.0.0.1:8000/health   # 应返回 {"status":"ok",...,"database":true}

# ============================================================
set -u
cd "$(dirname "$0")"

WB2API_DIR="${WB2API_DIR:-D:/workbuddy2api}"   # 可用环境变量覆盖（其他机器 workbuddy2api 所在路径）
PORT_8788="8788"
PORT_8787="8787"

# ---------- 读取 .env（去掉行尾 # 注释） ----------
strip_comment() { echo "$1" | cut -d# -f1 | tr -d ' \r'; }
LLM_PROVIDER=$(strip_comment "$(grep -E "^BIZ_LLM_PROVIDER=" .env 2>/dev/null | head -1 | cut -d= -f2-)")
CODEBUDDY_URL=$(strip_comment "$(grep -E "^BIZ_CODEBUDDY_BASE_URL=" .env 2>/dev/null | head -1 | cut -d= -f2-)")
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
  # Python 探测：优先项目虚拟环境（README 快速开始），未建 venv 时回退 PATH 中的 python
  if [ -x ".venv/Scripts/python.exe" ]; then PYTHON=".venv/Scripts/python.exe"
  elif [ -x ".venv/bin/python" ]; then PYTHON=".venv/bin/python"
  else PYTHON="python"; fi
  exec "$PYTHON" -m uvicorn main:app --host 127.0.0.1 --port 8000 2>&1 | tee -a logs/server_uvicorn.log
fi
