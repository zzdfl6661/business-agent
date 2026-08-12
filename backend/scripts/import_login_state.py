"""注入美团/点评登录态（CDP + 明文 cookie）并验证商家后台登录状态。

原理（与既有 login_state_transfer 方案一致）：
  1. Edge/Chrome 以 --remote-debugging-port 启动（持久化 profile）
  2. 通过 CDP 连接，把 cookies.json（明文）注入浏览器上下文
  3. 访问商家后台 URL，若最终 URL 不再落在 login/passport 页面 = 登录态生效

用法（backend 目录下）：
  python -m scripts.import_login_state --port 9222 ^
      --cookies "D:/z'z/新建/.../cookies.json" ^
      --url "https://e.dianping.com/app/merchant-platform/fe6031ae4f544c4"

安全：cookies.json 等价于账号密码，请勿入库/外传；本脚本不打印任何 cookie 值。
"""
from __future__ import annotations

import argparse
import json
import time

from playwright.sync_api import sync_playwright

DEFAULT_URL = (
    "https://e.dianping.com/app/merchant-platform/fe6031ae4f544c4"
    "?iUrl=Ly9lLmRpYW5waW5nLmNvbS9hcHAvbWVyY2hhbnQtd29ya2JlbmNoL2luZGV4Lmh0bWwjLw"
)


def norm(c: dict) -> dict:
    """cookies.json → playwright add_cookies 格式（字段规范化）。"""
    nc = {
        "name": c["name"],
        "value": c["value"],
        "domain": c["domain"],
        "path": c.get("path", "/"),
        "secure": bool(c.get("secure", False)),
        "httpOnly": bool(c.get("httpOnly", False)),
        "sameSite": c.get("sameSite", "Lax"),
    }
    exp = c.get("expires", -1)
    if exp and exp > 0:
        nc["expires"] = exp
    return nc


def validate_cookies(cookies: list[dict]) -> tuple[list[dict], list[str]]:
    """
    校验导出质量：
    - 返回 (通过校验的 cookie, 问题描述列表)
    - 已知坑：Edge profile 解密导出环节可能把 value 写坏（混入 NUL/控制字符），
      这类 value 注入后鉴权必失败 → 必须重新导出。
    """
    problems: list[str] = []
    good: list[dict] = []
    for c in cookies:
        v = c.get("value", "")
        bad = [ch for ch in v if ord(ch) < 32 and ch not in "\t\n"]
        if bad:
            problems.append(
                f"cookie `{c.get('name')}`（{c.get('domain')}）value 含 {len(bad)} 个控制字符 → 导出文件已损坏"
            )
        else:
            good.append(c)
    return good, problems


def main() -> None:
    ap = argparse.ArgumentParser(description="注入美团/点评登录态并验证")
    ap.add_argument("--port", type=int, default=9222, help="调试端口（默认 9222）")
    ap.add_argument("--cookies", required=True, help="cookies.json 路径")
    ap.add_argument("--url", default=DEFAULT_URL, help="待验证的后台 URL")
    args = ap.parse_args()

    with open(args.cookies, encoding="utf-8") as f:
        data = json.load(f)
    # 兼容两种导出结构：裸 list 或 {exported_at, count, cookies: [...]}
    cookies = data.get("cookies", data) if isinstance(data, dict) else data
    if not isinstance(cookies, list):
        print("[!] 无法识别的 cookies 文件结构（预期为 list 或 {cookies: [...]}）")
        return

    # 导出质量校验：value 含控制字符 = 导出损坏，直接拒绝注入
    good, problems = validate_cookies(cookies)
    if problems:
        print("[!] ❌ 导出文件质量校验未通过（value 含控制字符，解密导出环节损坏）：")
        for p in problems[:10]:
            print(f"    - {p}")
        print("    请从源机器重新导出 cookies.json（见 CROSS_MACHINE_LOGIN.md 的 extract_edge_cookies.py），")
        print("    本脚本不会注入已损坏的 cookie。")
        return

    normed = [norm(c) for c in good]
    # 只保留美团/点评系域名（避免注入无关站点的 cookie 触发异常）
    keep = [c for c in normed if any(d in c["domain"] for d in ("meituan.com", "dianping.com"))]
    print(f"[import] 读取 {len(normed)} 条，保留美团/点评系 {len(keep)} 条")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{args.port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        ctx.add_cookies(keep)
        print(f"[import] 已注入 {len(keep)} 条 cookie → 端口 {args.port}")

        page = ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)  # SPA 跳转
        final = page.url
        title = page.title()
        print(f"[verify] 目标: {args.url[:80]}…")
        print(f"[verify] 最终URL: {final[:120]}")
        print(f"[verify] 页面标题: {title[:60]}")

        blocked = any(k in final.lower() for k in ("login", "passport"))
        if blocked:
            print("[!] ⚠️ 仍跳转到登录页 → 登录态未生效（cookie 过期 / 设备风控）")
            print("    兜底：保持浏览器打开，在窗口里手动登录一次，登录态会自动持久化到 profile。")
        else:
            print("[✓] ✅ 已处于登录态！可继续后续自动化任务。")
        page.close()
        # 注意：不关闭 browser（保持调试实例，profile 持久化）


if __name__ == "__main__":
    main()
