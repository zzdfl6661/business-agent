# -*- coding: utf-8 -*-
"""#13-④ RAG golden set 命中率（与 #11 评测集共用）。

需要本地向量库（chroma_db）已 ingest；未就绪时跳过（不阻塞 CI）。
"""
import json
from pathlib import Path

import pytest

EVAL_FILE = Path(__file__).resolve().parent.parent / "data" / "eval" / "golden_set.json"
TOP_K = 5
MIN_HIT_RATE = 50.0  # 命中率低于 50% 视为检索回归


def _rag_ready():
    try:
        from rag.retriever import get_vector_client

        return get_vector_client().count() > 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _rag_ready(), reason="向量库未就绪（chroma_db 缺失或未 ingest），跳过 RAG 评测")


def _run_eval() -> dict:
    from rag.retriever import get_vector_client

    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    client = get_vector_client()
    total = hit = 0
    for c in data["cases"]:
        expected = c.get("expected_sources", []) or []
        results = client.query(c["question"], top_k=TOP_K, exclude_types=["report"])
        got = {((r.get("metadata") or {}).get("source") or "") for r in results}
        if any(any(e in s for s in got) for e in expected):
            hit += 1
        total += 1
    return {"total": total, "hit": hit, "hit_rate": round(hit / total * 100, 1) if total else 0.0}


def test_golden_set_hit_rate():
    if not EVAL_FILE.exists():
        pytest.skip("评测集缺失（data/eval/golden_set.json）")
    stats = _run_eval()
    assert stats["total"] > 0
    assert stats["hit_rate"] >= MIN_HIT_RATE, (
        f"RAG 命中率 {stats['hit_rate']}% < {MIN_HIT_RATE}%（{stats['hit']}/{stats['total']}）——检索参数疑似回归，"
        "请运行 python scripts/eval_rag.py 查看失败用例"
    )
