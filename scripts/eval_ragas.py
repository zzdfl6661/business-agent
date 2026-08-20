# -*- coding: utf-8 -*-
"""用 Ragas 评估知识库召回质量。

该脚本专注于可稳定复现的 retrieval metrics：Context Precision / Context Recall。
answer faithfulness 与 answer relevancy 需要先运行完整对话生成答案，避免把一个
检索回归测试偷偷变成高成本 LLM 端到端测试。

安装（开发环境）：
    pip install -r requirements.txt -r requirements-dev.txt

运行：
    python scripts/eval_ragas.py --provider openai_compatible

说明：golden_set.json 当前保存 expected_sources 而不是标准答案；脚本会从向量库
按 expected source 取参考上下文，作为 Ragas 的 ground_truth，来源命中率仍由
scripts/eval_rag.py 独立计算，二者互补。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
EVAL_FILE = ROOT / "data" / "eval" / "golden_set.json"


def _load_ragas():
    try:
        from datasets import Dataset
        # Ragas 0.2.x still imports the VertexAI module path removed from
        # langchain-community 0.4.x.  The evaluator below uses our configured
        # OpenAI-compatible LLM, so provide a process-local compatibility shim
        # instead of downgrading the application's LangChain dependencies.
        import sys
        import types

        if "langchain_community.chat_models.vertexai" not in sys.modules:
            from langchain_openai import ChatOpenAI

            vertexai_module = types.ModuleType("langchain_community.chat_models.vertexai")
            vertexai_module.ChatVertexAI = ChatOpenAI
            sys.modules["langchain_community.chat_models.vertexai"] = vertexai_module

        from ragas import evaluate
        from ragas.metrics import context_precision, context_recall
    except ImportError as exc:
        raise RuntimeError(
            "Ragas 评测依赖未安装，请执行：pip install -r requirements.txt -r requirements-dev.txt"
        ) from exc
    return Dataset, evaluate, context_precision, context_recall


def _reference_contexts(client, question: str, expected_sources: list[str]) -> list[str]:
    """从预期来源取参考父块，避免把 source 文件名当作语义答案。"""
    refs: list[str] = []
    # Chroma 实现可以直接读取受控评测语料；这样不会因为 query 排名把参考文档漏掉。
    try:
        collection = client._store._collection  # noqa: SLF001 - 评测脚本专用
        raw = collection.get(include=["documents", "metadatas"])
        all_docs = raw.get("documents") or []
        all_meta = raw.get("metadatas") or []
        for source in expected_sources:
            refs.extend(
                str(doc).strip()
                for doc, meta in zip(all_docs, all_meta)
                if source in str((meta or {}).get("source", "")) and doc
            )
        if refs:
            return ["\n".join(refs)[:5000]]
    except Exception:
        pass
    for source in expected_sources:
        try:
            # golden set 用文件名子串（如“公司背景”），而 metadata 保存完整文件名；
            # Chroma 标量过滤是精确匹配，因此这里先取候选再做安全的子串筛选。
            hits = client.query(question, top_k=10, exclude_types=["report"], hybrid=True)
            hits = [h for h in hits if source in str((h.get("metadata") or {}).get("source", ""))]
        except Exception:
            hits = []
        refs.extend(str(h.get("content", "")).strip() for h in hits if h.get("content"))
    # Ragas 的 ground_truth 是字符串；多个父块合并但限制长度，避免评测本身撑大 token。
    return ["\n".join(refs)[:5000]] if refs else []


def run(top_k: int = 5, max_cases: int | None = None, provider: str | None = None) -> dict:
    Dataset, evaluate, context_precision, context_recall = _load_ragas()
    from rag.retriever import get_vector_client

    cases = json.loads(EVAL_FILE.read_text(encoding="utf-8")).get("cases", [])
    if max_cases:
        cases = cases[:max_cases]
    client = get_vector_client()
    rows: list[dict] = []
    skipped = 0
    for case in cases:
        question = case["question"]
        expected = case.get("expected_sources", []) or []
        retrieved = client.query(question, top_k=top_k, exclude_types=["report"])
        contexts = [str(h.get("content", ""))[:5000] for h in retrieved if h.get("content")]
        references = _reference_contexts(client, question, expected)
        if not contexts or not references:
            skipped += 1
            continue
        rows.append({
            "question": question,
            "contexts": contexts,
            "answer": "",  # 仅跑 context metrics；不伪造端到端回答
            "ground_truth": references[0],
        })
    if not rows:
        raise RuntimeError("没有可评测样本：请先构建 Chroma 向量库并检查 golden_set")

    dataset = Dataset.from_list(rows)
    evaluator_llm = None
    if provider:
        from config.llm_factory import create_llm

        evaluator_llm = create_llm(provider)

    kwargs = {"dataset": dataset, "metrics": [context_precision, context_recall]}
    if evaluator_llm is not None:
        # 兼容 Ragas 0.2 的 LangChain 集成；部分小版本直接接受 BaseChatModel。
        try:
            from ragas.llms import LangchainLLMWrapper

            evaluator_llm = LangchainLLMWrapper(evaluator_llm)
        except ImportError:
            pass
        kwargs["llm"] = evaluator_llm
    result = evaluate(**kwargs)
    scores = result.to_pandas().mean(numeric_only=True).to_dict()
    output = {
        "samples": len(rows),
        "skipped": skipped,
        "top_k": top_k,
        "metrics": {k: round(float(v), 4) for k, v in scores.items()},
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Ragas 检索质量评估")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--provider", default=None, help="评测 LLM provider；不填时使用 Ragas 默认配置")
    args = parser.parse_args()
    run(args.top_k, args.max_cases, args.provider)


if __name__ == "__main__":
    main()
