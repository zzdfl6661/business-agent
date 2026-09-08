from services.conversation_memory import build_model_context, normalize_memory, should_compact


def _messages(count: int, content: str = "这是较长的历史消息内容") -> list[dict]:
    return [
        {"sequence": index + 1, "role": "user" if index % 2 == 0 else "assistant", "content": content}
        for index in range(count)
    ]


def test_compaction_keeps_recent_complete_turns_and_marks_only_old_messages():
    messages = _messages(12, "门店经营分析上下文" * 40)
    compact = should_compact(messages, 0, threshold_tokens=100, recent_turns=2)
    assert [item["sequence"] for item in compact] == list(range(1, 9))
    assert messages[-1]["sequence"] == 12  # 原始消息没有被函数删除


def test_model_context_contains_memory_and_budgeted_recent_raw_messages():
    memory = {"current_goal": "分析 3 号店", "confirmed_facts": [{"fact": "GMV 下滑", "source": "message:2"}]}
    context = build_model_context(
        _messages(8, "最近轮次" * 30), memory,
        token_budget=500, recent_turns=2, memory_max_chars=1000,
    )
    assert "压缩的会话记忆" in context[0].content
    assert "分析 3 号店" in context[0].content
    assert context[-1].content == "最近轮次" * 30
    assert len(context) <= 9  # 摘要 + 原文；预算会优先保留最新消息


def test_invalid_memory_never_breaks_context_building():
    assert normalize_memory("not-json") == {
        "current_goal": "", "entities": {}, "confirmed_facts": [], "decisions": [],
        "open_questions": [], "user_corrections": [],
    }
