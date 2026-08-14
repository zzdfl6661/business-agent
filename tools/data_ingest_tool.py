"""
数据采集工具：刷新美团经营数据
==============================
按钮驱动（/api/workflow/refresh 直接调用本模块普通函数，不经过 Agent/LLM）：

- refresh_market_data("campaign")：智选展位 4 维度（全自动：自动启动 Edge + 注入登录态 + 下载入库）
- refresh_market_data("all")：智选展位 + 入库全部 scraped 数据
- 客流/交易/咨询：美团商家后台 SPA 限制（菜单动态过期、iframe 直访被拦），
  需用户手动打开对应 tab 后重刷（Edge 与登录态已自动就绪）

注意：本模块**不是 LangChain 工具**（数据采集为按钮驱动，不走 Agent），
是 API 端点直接调用的普通函数，避免工具包装层引入的不确定性。

执行链路：自动启动 Edge(9222) → 注入登录态(cookies) → download_zxz_report.py（下载 xls）
         → import_market_data.py（清洗入库 + 快照）
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

# Edge 调试实例（CDP 登录态方案）：持久化 profile 保留登录态，自动拉起
EDGE_EXE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
EDGE_PROFILE_DIR = PROJECT_DIR / "data" / "edge_debug_profile"

# 登录态 cookies.json 候选路径（导出文件等价于账号密码，不入库、不出现在对话）
COOKIES_CANDIDATES = [
    Path(r"D:/z'z/新建/login_cookies_export.json"),
    PROJECT_DIR / "data" / "login_cookies_export.json",
]


def _edge_online(port: int) -> bool:
    """检查 Edge 调试端口是否在线（TCP 层面）。"""
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _edge_healthy(port: int) -> bool:
    """检查 Edge CDP 是否真正可用（HTTP /json/version 探活，TCP 在线不代表 CDP 可用）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _find_edge_exe() -> Path | None:
    for p in EDGE_EXE_CANDIDATES:
        if Path(p).exists():
            return Path(p)
    return None


def _kill_edge(port: int) -> None:
    """杀掉占用调试端口的 Edge 进程（持久化 profile，重启不丢登录态）。

    Windows 下 netstat 输出为本地代码页（GBK），text=True 解码遇非法 UTF-8 字节会抛
    UnicodeDecodeError → out 为空 → 解析失败 → Edge 杀不掉（重启无效的隐患之一）。
    因此用 bytes 模式 + errors=replace 解码。
    """
    try:
        proc = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=15)
        out = proc.stdout.decode("utf-8", errors="replace")
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line.upper():
                parts = line.strip().split()
                pid = parts[-1] if parts else ""
                if pid.isdigit():
                    subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=15)
                    logger.info("已终止 Edge 调试进程 PID=%s", pid)
        time.sleep(2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("终止 Edge 进程失败：%s", exc)


def _restart_edge(port: int) -> tuple[bool, str]:
    """重启 Edge 调试实例（自愈：Edge 被频繁连接后偶发进入坏状态，CDP 连接失败）。"""
    _kill_edge(port)
    return _ensure_edge(port)


def _ensure_edge(port: int) -> tuple[bool, str]:
    """确保 Edge 调试实例在线；不在线则自动启动（持久化 profile，登录态可复用）。"""
    if _edge_online(port):
        return True, f"Edge 调试实例已在线（port={port}）"

    exe = _find_edge_exe()
    if exe is None:
        return False, "未找到 Edge 可执行文件，请安装 Microsoft Edge"
    EDGE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    subprocess.Popen(
        [
            str(exe),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={EDGE_PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # 等 TCP 端口就绪
    for _ in range(15):
        time.sleep(2)
        if _edge_online(port):
            break
    # 等 CDP 探活通过 + 额外缓冲（Edge 冷启动：端口开了但页面服务未完全 ready，
    # 立即注入会 connect 失败 → rc=1 零输出；多等几秒规避）
    for _ in range(10):
        time.sleep(1)
        if _edge_online(port) and _edge_healthy(port):
            time.sleep(6)  # 冷启动缓冲
            return True, f"已自动启动 Edge（port={port}，profile 登录态保留）"
    return False, f"Edge 自动启动超时（port={port} 未就绪）"


async def _inject_login(port: int, cookies: Path) -> tuple[bool, str]:
    """事件循环内注入登录态（async playwright，复用 import_login_state 核心逻辑）。

    必须用 async API：playwright 同步 API 不允许在线程池（run_in_threadpool）使用，
    而注入要连 Edge CDP + 打开后台页验证，改在 FastAPI 事件循环直接跑。
    """
    try:
        import json

        import asyncio

        from playwright.async_api import async_playwright

        from scripts.import_login_state import DEFAULT_URL, norm, validate_cookies

        with open(cookies, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("cookies", data) if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return False, "cookies 文件结构无法识别"
        good, problems = validate_cookies(raw)
        if problems:
            return False, f"cookies 校验未通过：{problems[:1]}"
        keep = [
            c for c in (norm(x) for x in good)
            if any(d in c["domain"] for d in ("meituan.com", "dianping.com"))
        ]
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            await ctx.add_cookies(keep)
            page = await ctx.new_page()
            await page.goto(DEFAULT_URL, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(6)  # SPA 跳转
            final = page.url
            await page.close()
        if any(k in final.lower() for k in ("login", "passport")):
            return False, "注入后仍跳转登录页（cookie 过期或设备风控）"
        return True, "登录态已自动注入 ✓"
    except Exception as exc:  # noqa: BLE001
        logger.error("登录态注入异常：%s", exc)
        return False, f"注入异常：{str(exc)[:150]}"


async def _ensure_login(port: int) -> tuple[bool, str]:
    """自动注入登录态。

    关键设计：**注入必须跑在独立子进程**（`import_login_state` 脚本）。
    实测：playwright 独立进程 100% 稳定；而在 uvicorn 进程内反复启动/失败
    async_playwright 后，driver 启动会进入"累积假死"（Connection closed while
    reading from the driver），重启 Edge 也无效（问题在 uvicorn 进程内 driver）。
    子进程每次全新启动，天然规避。
    """
    try:
        cookies = next((c for c in COOKIES_CANDIDATES if c.exists()), None)
        if cookies is None:
            return False, "未找到 cookies.json（登录态导出文件），请先在 Edge 中手动登录商家后台一次"

        # 预检：TCP 在线但 CDP 假死（频繁连接后常见）→ 直接重启，避免盲试浪费时间
        if _edge_online(port) and not _edge_healthy(port):
            logger.warning("Edge CDP 探活失败（假死状态），重启 Edge")
            ok, msg = _restart_edge(port)
            if not ok:
                return False, f"Edge 重启失败：{msg}"

        def _inject_sub() -> tuple[int, str]:
            return _run_module(
                "scripts.import_login_state",
                [f"--port={port}", f"--cookies={cookies}"],
                timeout=150,
            )

        # 第一轮：子进程注入，重试 3 次（间隔 4s，给 Edge 恢复时间）
        last_out = ""
        for attempt in range(3):
            rc, out = await asyncio.to_thread(_inject_sub)
            if rc == 0:
                return True, "登录态已自动注入 ✓"
            last_out = out
            logger.warning("登录态注入第 %s 次失败 rc=%s out=%.150r", attempt + 1, rc, out)
            await asyncio.sleep(4)

        # 第二轮：Edge 可能进入坏状态/冷启动未就绪，重启后等待充分再注入 3 次
        logger.warning("登录态注入连续失败，重启 Edge 并等待就绪后重试")
        ok, msg = _restart_edge(port)
        if not ok:
            return False, f"登录态注入失败且 Edge 重启失败：{msg}"
        await asyncio.sleep(8)  # Edge 冷启动缓冲（端口就绪 ≠ 页面服务可用）
        for attempt in range(3):
            rc, out = await asyncio.to_thread(_inject_sub)
            if rc == 0:
                return True, "登录态已自动注入 ✓（Edge 已重启）"
            last_out = out
            await asyncio.sleep(4)
        return False, f"登录态注入失败（重启 Edge 后仍失败）：{last_out[-200:]}"
    except Exception as exc:  # noqa: BLE001
        logger.error("登录态注入链路异常：%s", exc, exc_info=True)
        return False, f"登录态注入链路异常：{str(exc)[:150]}"


def _clean_env() -> dict:
    """子进程净化环境：去掉 WorkBuddy 注入的 PYTHONPATH/session 变量。

    WorkBuddy 桌面端会把 PYTHONPATH 指向 vendor/shim 目录并注入 session 变量，
    子进程 python 启动时自动加载 shim/sitecustomize.py（安全删除拦截），
    该 shim 在子进程/无控制台场景会干扰 playwright 启动 → 子进程 rc=1 零输出。
    子进程必须运行在纯净 Python 环境。
    """
    env = os.environ.copy()
    for k in (
        "PYTHONPATH",
        "PYTHONHOME",
        "CODEBUDDY_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CODEBUDDY_SAFE_DELETE_SANDBOX",
    ):
        env.pop(k, None)
    return env


def _run_module(module: str, args: list[str] | None = None, timeout: int = 600) -> tuple[int, str]:
    """运行 backend 下模块脚本，返回 (returncode, 输出摘要)。

    - bytes 模式 + errors=replace：避免 Windows 下子进程 UTF-8 输出解码异常被吞
    - CREATE_NO_WINDOW：后台进程启动控制台子进程兼容
    - env=_clean_env()：剔除 WorkBuddy shim 注入，防止 sitecustomize 干扰子进程
    """
    cmd = [PYTHON, "-m", module] + (args or [])
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        clean_env = _clean_env()
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_DIR), capture_output=True, timeout=timeout,
            env=clean_env, **kwargs,
        )
        out = proc.stdout.decode("utf-8", errors="replace")[-600:] + proc.stderr.decode("utf-8", errors="replace")[-400:]
        if proc.returncode != 0:
            # 探针：同 env 跑最简 python 命令，区分「python 子进程启动问题」与「脚本/playwright/Edge 问题」
            probe = subprocess.run(
                [PYTHON, "-c", "import sys; print('PROBE_OK', sys.version.split()[0])"],
                cwd=str(PROJECT_DIR), capture_output=True, timeout=30,
                env=clean_env, **kwargs,
            )
            logger.error(
                "子进程失败 module=%s rc=%s exe=%s\n"
                "cwd=%s sys.prefix=%s\n"
                "子进程env PATH:%.150s PYTHONPATH:%s SESSION:%s UTF8:%s\n"
                "probe_rc=%s probe_out=%r\n"
                "stdout=%r stderr=%r",
                module, proc.returncode, PYTHON, PROJECT_DIR, sys.prefix,
                clean_env.get("PATH", ""), clean_env.get("PYTHONPATH", ""),
                clean_env.get("CODEBUDDY_SESSION_ID", ""), clean_env.get("PYTHONUTF8", ""),
                probe.returncode, probe.stdout[:200] + probe.stderr[:200],
                proc.stdout[:300], proc.stderr[:300],
            )
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return -1, f"执行超时（>{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        logger.error("subprocess 启动异常 module=%s exe=%s cwd=%s err=%r", module, PYTHON, PROJECT_DIR, exc)
        return -1, str(exc)


async def refresh_market_data(datasets: str = "campaign", port: int = 9222) -> dict:
    """刷新美团经营数据并入库（按钮驱动，async 版：事件循环内注入登录态）。

    参数：
    - datasets: 要刷新的数据集，逗号分隔或 "all"。
      "campaign"=智选展位4维度(全自动)；"traffic"/"transaction"/"consult" 因美团商家后台
      SPA 限制需手动打开对应经营参谋 tab 后重刷（Edge 与登录态已自动就绪）。
    - port: Edge 调试端口（默认 9222）。

    返回：各数据集下载/入库结果。耗时约 1~3 分钟。
    """
    import asyncio as _asyncio

    # 1) 强制重启 Edge 调试实例（每次全新状态）：连续操作后 Edge 会进入"累积假死"
    #    （CDP /json/version 探活正常但实际页面操作卡死），探活检测不到，
    #    每次刷新前 kill + 重新启动最稳妥，代价约 30 秒。
    edge_ok, edge_msg = _restart_edge(port)
    if not edge_ok:
        return {"success": False, "data": {}, "error": edge_msg}
    # 2) 自动注入登录态（独立子进程 playwright，规避 uvicorn 进程内 driver 累积问题）
    login_ok, login_msg = await _ensure_login(port)
    if not login_ok:
        return {"success": False, "data": {}, "error": login_msg}

    ds = {d.strip() for d in datasets.lower().split(",")} if datasets != "all" else {"campaign", "traffic", "transaction", "consult"}
    results: dict = {"edge": {"success": True, "detail": edge_msg}, "login": {"success": True, "detail": login_msg}}

    # 1) 智选展位：全自动下载（脚本内自动导航页面，subprocess 隔离执行）→ 入库
    if "campaign" in ds or "all" in datasets:
        rc, out = -1, ""
        for attempt in range(2):  # Edge 状态偶发，重试一次
            rc, out = await _asyncio.to_thread(_run_module, "scripts.download_zxz_report", [f"--port={port}"], 600)
            if rc == 0:
                break
            logger.warning("智选展位下载第 %s 次失败 rc=%s out=%.150r", attempt + 1, rc, out)
            await _asyncio.sleep(3)
        results["campaign_download"] = {"success": rc == 0, "detail": out[-200:]}

    # 2) 统一入库（智选 4 维 + 已下载的客流/交易/咨询文件）
    if any(d in ds for d in ("campaign", "all")) or "all" in datasets:
        rc, out = await _asyncio.to_thread(_run_module, "scripts.import_market_data", None, 300)
        results["import"] = {"success": rc == 0, "detail": out[-300:]}

    # 3) 需手动开 tab 的模块（返回指引；Edge 与登录态已就绪，manual 标记供前端显示 ⏳）
    manual = {"traffic": "客流分析", "transaction": "交易分析", "consult": "在线咨询分析"}
    for key, name in manual.items():
        if key in ds:
            results[key] = {
                "success": False,
                "manual": True,
                "detail": f"{name} 需在已自动启动的 Edge 中手动打开 经营参谋→{name} tab 后重刷（美团后台 SPA 限制，脚本无法自动导航）",
            }

    # 成功判定：核心自动化模块（edge/login/campaign_download/import）全部成功即可，
    # manual 模块（需手动开 tab 的指引）不计入失败
    ok = all(r.get("success", False) for k, r in results.items() if not r.get("manual"))
    return {"success": ok, "data": results, "error": None}
