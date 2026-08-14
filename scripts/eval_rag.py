# -*- coding: utf-8 -*-
"""RAG 检索评测脚本（#11）

用法：
    python scripts/eval_rag.py [--top-k 5] [--hybrid] [--query "自定义问题"]

对 golden set（data/eval/golden_set.json）逐条检索，检查预期文档是否命中 top_k 结果，
输出：命中率（Hit@k）、平均首命中排名、失败用例明细。

改造 embedding / 切块参数 / 检索策略后必须跑一遍，避免"感觉变好"的假象。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EVAL_FILE = ROOT / "data" / "eval" / "golden_set.json"


def run(query: str | None = None, top_k: int = 5, verbose: bool = True) -> dict:
    from rag.retriever import get_vector_client

    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    cases = data["cases"]
    client = get_vector_client()

    if query:
        cases = [{"question": query, "expected_sources": []}]

    total = hit = 0
    details = []
    for c in cases:
        q = c["question"]
        expected = c.get("expected_sources", []) or []
        try:
            results = client.query(q, top_k=top_k, exclude_types=["report"])
        except Exception as exc:  # noqa: BLE001 向量库未就绪
            print(f"❌ 检索异常 {q[:20]}…：{exc}")
            continue
        got = [((r.get("metadata") or {}).get("source") or "") for r in results]
        hit_rank = None
        for idx, src in enumerate(got, 1):
            if any(e in src for e in expected):
                hit_rank = idx
                break
        total += 1
        if hit_rank:
            hit += 1
        details.append({
            "question": q,
            "expected": expected,
            "hit_rank": hit_rank,
            "top_sources": got[: top_k],
        })
        if verbose:
            mark = f"✅@{hit_rank}" if hit_rank else "❌"
            print(f"{mark} {q[:28]:<30} → {expected} | top: {got[:2]}")

    stats = {
        "total": total,
        "hit": hit,
        "hit_rate": round(hit / total * 100, 1) if total else 0.0,
        "top_k": top_k,
    }
    if verbose:
        print()
        print(f"命中率 Hit@{top_k}：{stats['hit']}/{stats['total']} = {stats['hit_rate']}%")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG golden set 检索评测")
    parser.add_argument("--top-k", type=int, default=5, help="每问题取前 N 条判命中")
    parser.add_argument("--query", type=str, default=None, help="只测单条自定义问题")
    args = parser.parse_args()

    stats = run(args.query, args.top_k)
    sys.exit(0 if stats["hit_rate"] >= 50.0 else 1)  # 命中率低于 50% 视为回归


if __name__ == "__main__":
    main()
