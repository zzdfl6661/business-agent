"""
下载「智选展位 → 数据报告」四个维度的数据明细（xls）
======================================================
页面右上角「下载数据明细」按钮直接导出当前维度表格（.xls），
比 DOM 翻页爬取高效。四维度切换后各下载一次，文件名加维度后缀避免覆盖。

用法（Edge 9222 已打开智选展位→数据报告页）：
  python -m scripts.download_zxz_report [--port 9222]

输出：backend/data/scraped/<时间段>_<维度>.xls
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "scraped"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DIMENSIONS = ["分时段查看", "分推广查看", "分人群查看", "分创意查看"]


def find_report_frame(page):
    for f in page.frames:
        if "data-report" in f.url or "peon-cpm-ncpm" in f.url:
            return f
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args()

    saved: list[Path] = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{args.port}")
        page = browser.contexts[0].pages[0]
        frame = find_report_frame(page)
        if not frame:
            # 自动导航到智选展位数据报告页（登录态已持久化，iframe 直访）
            print("[*] 未找到 data-report iframe，自动导航…")
            page.goto("https://e.dianping.com/app/peon-cpm-ncpm/html/data-report.html",
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            frame = find_report_frame(page)
        if not frame:
            print("[!] 仍无法定位 data-report iframe，请确认登录态有效（Edge 9222）")
            return
        frame.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        for dim in DIMENSIONS:
            # 1) 切换维度（force click：页面 header 菜单可能遮挡 radio，绕过 pointer 拦截）
            try:
                btn = frame.get_by_text(dim, exact=True).first
                if await_visible(btn):
                    btn.click(force=True)
                    time.sleep(3)
                else:
                    print(f"[skip] 维度按钮不可见: {dim}")
                    continue
            except Exception as exc:
                print(f"[skip] {dim} 切换失败: {exc}")
                continue

            # 2) 点击「下载数据明细」并保存（带维度后缀；force 防遮挡；失败重试 2 次）
            dl_btn = frame.get_by_text("下载数据明细", exact=True).first
            downloaded = False
            for attempt in range(3):
                try:
                    if not await_visible(dl_btn, 8000):
                        print(f"[retry{attempt}] 下载按钮未出现，等待页面渲染…")
                        time.sleep(3)
                        continue
                    with page.expect_download(timeout=60000) as dl_info:
                        dl_btn.click(force=True)
                    dl = dl_info.value
                    name = dl.suggested_filename  # 形如 2026-08-03_2026-08-09.xls
                    dest = OUTPUT_DIR / f"{Path(name).stem}_{dim}.xls"
                    dl.save_as(str(dest))
                    saved.append(dest)
                    print(f"[OK] {dim}: {dest.name} ({dest.stat().st_size} bytes)")
                    downloaded = True
                    break
                except Exception as exc:
                    print(f"[FAIL] {dim} 第{attempt + 1}次: {str(exc)[:120]}")
                    time.sleep(2)
            if not downloaded:
                print(f"[SKIP] {dim} 连续失败，跳过")
            time.sleep(1)

    print(f"\n✅ 完成，共下载 {len(saved)} 个文件:")
    for s in saved:
        print("   ", s)
    if not saved:
        print("[!] 未下载到任何文件（页面状态或登录态异常）")
        sys.exit(1)


def await_visible(locator, timeout_ms: int = 5000) -> bool:
    """等待元素可见（短超时，避免长阻塞）。"""
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    main()
