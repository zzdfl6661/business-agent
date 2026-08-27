import asyncio
import json

from api import chat


class _ClarificationAgent:
    async def astream_events(self, _inputs, version):
        assert version == "v2"
        yield {
            "event": "on_chain_end",
            "metadata": {"langgraph_node": "clarification"},
            "data": {"output": {"final_report": "clarification text"}},
        }


async def _read_stream(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def test_stream_renders_clarification_in_done_event(monkeypatch):
    monkeypatch.setattr(chat, "agent", _ClarificationAgent())
    monkeypatch.setattr(chat, "_save_session_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat, "audit", lambda *args, **kwargs: None)

    response = asyncio.run(chat.chat_stream(chat.ChatRequest(question="分析某门店并通知店长")))
    body = asyncio.run(_read_stream(response))

    done_data = next(line[6:] for line in body.splitlines() if line.startswith("data: {") )
    assert json.loads(done_data)["report"] == "clarification text"
