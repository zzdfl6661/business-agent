"""
数据采集工具：对话触发刷新美团经营数据
======================================
将下载脚本封装为 Agent 工具（A 方案：对话命令触发）：

- refresh_market_data("campaign")：智选展位 4 维度（全自动，依赖 Edge 9222 登录态）
- refresh_market_data("all")：智选展位 + 入库全部 scraped 数据
- 客流/交易/咨询：需用户手动打开对应 tab 后由 Agent 接管（脚本化程度低，返回指引）

执行链路：download_zxz_report.py（下载 xls）→ import_market_data.py（清洗入库 + 快照）
"""
from __future__ import annotations

import logging
import socket
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _edge_online(port: int) -> bool:
    """检查 Edge 调试实例是否在线（登录态前提）。"""
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _run_module(module: str, args: list[str] | None = None, timeout: int = 600) -> tuple[int, str]:
    """运行 backend 下模块脚本，返回 (returncode, 输出摘要)。"""
    cmd = [PYTHON, "-m", module] + (args or [])
    try:
        proc = subprocess.run(cmd, cwd=str(BACKEND_DIR), capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "")[-600:] + (proc.stderr or "")[-400:]
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return -1, f"执行超时（>{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


@tool
def refresh_market_data(datasets: str = "campaign", port: int = 9222) -> dict:
    """刷新美团经营数据并入库（对话触发的数据采集）。

    参数：
    - datasets: 要刷新的数据集，逗号分隔或 "all"。
      "campaign"=智选展位4维度(全自动)；"traffic"/"transaction"/"consult" 需先手动打开
      对应经营参谋页面 tab（Agent 接管下载，当前版本返回操作指引）。
    - port: Edge 调试端口（默认 9222）。

    返回：各数据集下载/入库结果。耗时约 1~3 分钟。
    """
    if not _edge_online(port):
        return {"success": False, "data": {}, "error": f"Edge 调试实例({port})未在线，请先启动项目（登录态）再刷新"}

    ds = {d.strip() for d in datasets.lower().split(",")} if datasets != "all" else {"campaign", "traffic", "transaction", "consult"}
    results: dict = {}

    # 1) 智选展位：全自动下载（脚本内自动导航页面，subprocess 隔离执行）→ 入库
    if "campaign" in ds or "all" in datasets:
        rc, out = _run_module("scripts.download_zxz_report", [f"--port={port}"], timeout=600)
        results["campaign_download"] = {"success": rc == 0, "detail": out[-200:]}

    # 2) 统一入库（智选 4 维 + 已下载的客流/交易/咨询文件）
    if any(d in ds for d in ("campaign", "all")) or "all" in datasets:
        rc, out = _run_module("scripts.import_market_data", timeout=300)
        results["import"] = {"success": rc == 0, "detail": out[-300:]}

    # 3) 需手动开 tab 的模块（返回指引）
    manual = {"traffic": "客流分析", "transaction": "交易分析", "consult": "在线咨询分析"}
    for key, name in manual.items():
        if key in ds:
            results[key] = {
                "success": False,
                "detail": f"{name} 需用户手动打开经营参谋→{name} tab 后由 Agent 接管下载（当前版本脚本未完全自动化）",
            }

    ok = all(r.get("success", False) for k, r in results.items() if k != "import")
    return {"success": ok, "data": results, "error": None}
