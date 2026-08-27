"""统一采集美团经营报表。

campaign 可自动导航；traffic/transaction/consult 复用用户已在调试 Edge 中打开的
对应页面，按页面上的“下载明细/下载数据/导出”按钮采集。找不到页面时明确失败，
不把磁盘旧文件伪装成新下载。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "scraped"

ADAPTERS = {
    "traffic": {
        "page_words": ("客流分析",),
        "names": ("客流分析_1_客流数据", "客流分析_2_客流来源", "客流分析_3_引流用户数据"),
    },
    "transaction": {
        "page_words": ("交易分析", "商品交易"),
        "names": ("商品交易数据", "门店交易数据"),
    },
    "consult": {
        "page_words": ("在线咨询分析", "在线咨询"),
        "names": ("在线咨询数据", "分时段咨询数据"),
    },
}


def _page_text(page: Page) -> str:
    try:
        return f"{page.url}\n{page.title()}\n{page.locator('body').inner_text(timeout=3000)[:10000]}"
    except Exception:
        return page.url


def _find_page(pages: list[Page], words: tuple[str, ...]) -> Page | None:
    for page in reversed(pages):
        text = _page_text(page)
        if any(word in text for word in words) and not any(x in page.url.lower() for x in ("login", "passport")):
            return page
    return None


def _download_open_page(page: Page, dataset: str) -> list[str]:
    adapter = ADAPTERS[dataset]
    candidates = page.locator("button, a").filter(has_text=re.compile(r"下载明细|下载数据|导出"))
    visible = [candidates.nth(i) for i in range(candidates.count()) if candidates.nth(i).is_visible()]
    if not visible:
        raise RuntimeError("页面已找到，但未找到可见的下载/导出按钮")

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved: list[str] = []
    # 只点击该页面声明数量以内的导出按钮，避免误触其他页面动作。
    for index, locator in enumerate(visible[: len(adapter["names"])]):
        try:
            with page.expect_download(timeout=45000) as info:
                locator.click(timeout=15000)
            download = info.value
            suffix = Path(download.suggested_filename).suffix or ".xlsx"
            target = OUT / f"{adapter['names'][index]}-{stamp}{suffix}"
            download.save_as(target)
            saved.append(target.name)
        except Exception as exc:
            # 某些“导出”按钮先打开筛选弹窗；单个按钮失败不掩盖其他成功文件。
            saved.append(f"ERROR:{index + 1}:{str(exc)[:100]}")
    actual = [name for name in saved if not name.startswith("ERROR:")]
    if not actual:
        raise RuntimeError("导出按钮未产生下载文件")
    return actual


def _download_campaign(port: int) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.download_zxz_report", f"--port={port}"],
        cwd=ROOT,
        capture_output=True,
        timeout=600,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")[-300:]
        raise RuntimeError(detail or f"campaign downloader rc={proc.returncode}")
    return ["智选展位4维度"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datasets", default="campaign")
    parser.add_argument("--port", type=int, default=9222)
    args = parser.parse_args()
    requested = ({"campaign", "traffic", "transaction", "consult"}
                 if args.datasets == "all" else {x.strip() for x in args.datasets.split(",") if x.strip()})
    result: dict[str, dict] = {}
    if "campaign" in requested:
        try:
            result["campaign"] = {"success": True, "files": _download_campaign(args.port)}
        except Exception as exc:
            result["campaign"] = {"success": False, "error": str(exc)[:300]}

    browser_datasets = requested & set(ADAPTERS)
    if browser_datasets:
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{args.port}")
                pages = [page for context in browser.contexts for page in context.pages]
                for dataset in sorted(browser_datasets):
                    page = _find_page(pages, ADAPTERS[dataset]["page_words"])
                    if page is None:
                        result[dataset] = {
                            "success": False,
                            "manual": True,
                            "error": f"未找到已打开的{ADAPTERS[dataset]['page_words'][0]}页面",
                        }
                        continue
                    try:
                        result[dataset] = {"success": True, "files": _download_open_page(page, dataset)}
                    except Exception as exc:
                        result[dataset] = {"success": False, "manual": True, "error": str(exc)[:300]}
        except Exception as exc:
            for dataset in browser_datasets:
                result.setdefault(dataset, {"success": False, "error": f"连接调试 Edge 失败：{str(exc)[:200]}"})

    print("RESULT_JSON=" + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result and all(v.get("success") for v in result.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
