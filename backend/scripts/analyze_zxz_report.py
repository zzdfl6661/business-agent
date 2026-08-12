"""
分析智选展位四维度数据明细（xls → pandas 清洗 → 概览 + 清洗后 CSV）
=========================================================================
- 读取 data/scraped/2026-08-03_2026-08-09_*.xls 四个文件
- 数值清洗：千分位 '10,952.77' → float；'2.91%' → float(百分比数值)
- 输出：每个维度的 shape/列名/总计行/Top 5
- 保存清洗后 CSV 到 data/scraped/cleaned/
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "scraped"
OUT_DIR = DATA_DIR / "cleaned"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FILES = {
    "分时段查看": DATA_DIR / "2026-08-03_2026-08-09_分时段查看.xls",
    "分推广查看": DATA_DIR / "2026-08-03_2026-08-09_分推广查看.xls",
    "分人群查看": DATA_DIR / "2026-08-03_2026-08-09_分人群查看.xls",
    "分创意查看": DATA_DIR / "2026-08-03_2026-08-09_分创意查看.xls",
}

# 数值类列（需要清洗千分位/百分比）
NUM_COLS = ["花费", "曝光", "点击", "千次曝光均价", "点击率", "感兴趣", "预约及意向",
            "收藏", "查看团购", "查看优惠促销", "支付订单量", "团购订单量",
            "间接预约及意向", "间接支付订单量", "间接团购订单量",
            "间接在线咨询沟通量", "新客感兴趣量", "分享"]


def clean_num(s) -> float | None:
    if pd.isna(s) or s in ("", "-", "/"):
        return None
    s = str(s).replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def load(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path)
    for col in NUM_COLS:
        if col in df.columns:
            df[col] = df[col].apply(clean_num)
    return df


def main():
    summary = {}
    for dim, path in FILES.items():
        if not path.exists():
            print(f"[skip] 不存在: {path.name}")
            continue
        df = load(path)
        summary[dim] = df
        print("=" * 70)
        print(f"📊 {dim}: {df.shape[0]} 行 × {df.shape[1]} 列")
        print("   列名:", "、".join(df.columns[:8]) + ("…" if len(df.columns) > 8 else ""))
        # 总计行
        total = df[df.iloc[:, 0].astype(str).str.contains("总计")]
        if not total.empty:
            t = total.iloc[0]
            print(f"   总计: 花费={t.get('花费')} 曝光={t.get('曝光')} 点击={t.get('点击')} "
                  f"点击率={t.get('点击率')} 支付订单={t.get('支付订单量')}")
        # 非总计 Top 5（按花费）
        body = df[~df.iloc[:, 0].astype(str).str.contains("总计")]
        if "花费" in body.columns and not body.empty:
            top = body.sort_values("花费", ascending=False).head(5)
            print("   花费 Top5:")
            for _, r in top.iterrows():
                name = str(r.iloc[0])[:22]
                print(f"     {name:24s} 花费={r['花费']} 曝光={r['曝光']} 点击={r['点击']} 点击率={r['点击率']}")
        # 保存清洗后 CSV
        out = OUT_DIR / f"{path.stem}.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"   ✅ 已保存清洗后 CSV: {out.name}")

    # 简单汇总对比
    print("\n" + "=" * 70)
    print("📈 四维度总计对比（花费/曝光/点击）")
    for dim, df in summary.items():
        total = df[df.iloc[:, 0].astype(str).str.contains("总计")]
        if not total.empty:
            t = total.iloc[0]
            print(f"   {dim:8s} 花费={t.get('花费'):>10} 曝光={t.get('曝光'):>10} 点击={t.get('点击'):>8} 点击率={t.get('点击率')}")


if __name__ == "__main__":
    main()
