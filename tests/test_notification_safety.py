import asyncio
import json
from types import SimpleNamespace

import pytest

from config.settings import settings
from integrations.dingtalk_dws import DingTalkDWSHostClient, DingTalkDWSError
from integrations.dingtalk_mcp import DingTalkMCPClient
from services.contacts import _read_rows, _resolve_store, preview_contact_import
from services.notifications import (
    _dispatch_key,
    confirm_notification_plan,
    update_notification_text,
    validate_message_text,
    validate_notification_facts,
)


def test_notification_text_rejects_phone_and_oversize():
    with pytest.raises(ValueError, match="手机号"):
        validate_message_text("请联系 13800000000 跟进")
    with pytest.raises(ValueError, match="不能超过"):
        validate_message_text("a" * 1201)
    assert validate_message_text("【经营提醒】\n请结合现场情况跟进。").startswith("【经营提醒】")


def test_notification_numbers_must_come_from_analysis_snapshot():
    payload = {"report_sections": {"metrics": ["GMV 1200 元，环比 -8.5%"]}}
    assert validate_notification_facts("一、关键情况\n- GMV 1200 元，环比 -8.5%\n\n1. 核查现场", payload)
    with pytest.raises(ValueError, match="无法追溯"):
        validate_notification_facts("建议本周提升 30%", payload)


def test_contacts_csv_supports_real_header_shape():
    data = (
        "门店名称,分店,店长电话,美团门店全名,店长姓名\n"
        "上海正大广场店,正大,19925803249,迷之好玩·魔法互动·机械密室(正大店),金珑\n"
    ).encode("utf-8")
    rows = _read_rows("店长信息库.csv", data)
    assert rows[0]["美团门店全名"].startswith("迷之好玩")
    assert rows[0]["店长姓名"] == "金珑"


def test_contact_preview_masks_phone_and_detects_duplicate_store():
    from database.models import Store

    class _Scalars:
        def all(self):
            return [Store(id=1, store_code="s1", store_name="上海正大广场店", search_keyword="正大")]

    class _Session:
        def execute(self, _):
            return SimpleNamespace(scalars=lambda: _Scalars())

    data = (
        "门店名称,店长电话,店长姓名\n"
        "上海正大广场店,19900001111,甲\n"
        "上海正大广场店,19900001111,乙\n"
    ).encode()
    result = preview_contact_import("联系人.csv", data, _Session())
    serialised = json.dumps(result, ensure_ascii=False)
    assert "19900001111" not in serialised
    assert result["valid"] == 1
    assert "重复" in result["rows"][1]["errors"][0]


def test_full_store_name_can_match_database_keyword():
    from database.models import Store

    store = Store(id=1, store_code="s1", store_name="正大店", search_keyword="正大")
    row = {"美团门店全名": "迷之好玩·魔法互动·机械密室(正大店)"}
    assert _resolve_store(row, [store]).id == 1


def test_full_store_name_wins_over_shared_location_keyword():
    from database.models import Store

    mystery = Store(
        id=1, store_code="s1", store_name="迷之好玩魔法互动机械密室正大店", search_keyword="正大"
    )
    bb_boom = Store(
        id=2, store_code="s2", store_name="bb boom运动游戏馆正大店", search_keyword="正大"
    )
    row = {"美团门店全名": "迷之好玩·魔法互动·机械密室(正大店)", "分店": "正大"}
    assert _resolve_store(row, [mystery, bb_boom]).id == 1


def test_dry_run_keeps_intended_recipient_but_never_calls_network(monkeypatch):
    import services.notifications as notices

    plan = SimpleNamespace(
        plan_id="notice_1", analysis_run_id="run_1", session_id="session_1", store_id=1,
        intended_recipient="王店长（测试店店长）", effective_recipient="朱兴福",
        message_text="GMV 1200 元\n1. 核查现场", status="pending_approval",
        result_detail=None, created_at=None, approved_at=None, dispatched_at=None,
    )
    session = SimpleNamespace(commit=lambda: None)
    monkeypatch.setattr(notices, "get_notification_plan", lambda *_: plan)
    monkeypatch.setattr(notices, "_analysis_payload_for_plan", lambda *_: {"metrics": ["GMV 1200 元"]})
    monkeypatch.setattr(settings, "dingtalk_enabled", False)
    monkeypatch.setattr(settings, "dingtalk_mode", "dry_run")

    result = confirm_notification_plan("notice_1", session)
    assert result["status"] == "simulated"
    assert result["intended_recipient"].startswith("王店长")
    assert result["effective_recipient"] == "朱兴福"
    assert result["result"]["network_called"] is False
    assert confirm_notification_plan("notice_1", session)["status"] == "simulated"


def test_disabled_mcp_never_connects(monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_enabled", False)
    monkeypatch.setattr(settings, "dingtalk_mode", "dry_run")
    result = asyncio.run(DingTalkMCPClient().describe_capabilities())
    assert result == {"status": "disabled", "tools": []}


def test_live_capability_check_allows_initial_recipient_lookup(monkeypatch):
    for name, value in {
        "dingtalk_enabled": True,
        "dingtalk_mode": "live",
        "dingtalk_mcp_transport": "stdio",
        "dingtalk_client_id": "ding-client-id",
        "dingtalk_client_secret": "client-secret",
        "dingtalk_robot_code": "robot-code",
        "dingtalk_live_recipient_user_id": "",
    }.items():
        monkeypatch.setattr(settings, name, value)

    DingTalkMCPClient()._validate_live_config(require_recipient=False)
    with pytest.raises(Exception, match="LIVE_RECIPIENT_USER_ID"):
        DingTalkMCPClient()._validate_live_config()


def test_mcp_adapter_discovers_and_calls_in_memory_server(monkeypatch):
    """SDK in-memory Client covers discovery, parameters and adapter boundaries."""
    from mcp.server import MCPServer

    calls = []
    server = MCPServer("test-dingtalk")

    @server.tool(name="find_user")
    def find_user(mobile: str) -> dict:
        calls.append(("find", mobile))
        return {"user_id": "ding-test-user"}

    @server.tool(name="send_text")
    def send_text(user_id: str, text: str) -> dict:
        calls.append(("send", user_id, text))
        return {"ok": True}

    for name, value in {
        "dingtalk_enabled": True,
        "dingtalk_mode": "live",
        "dingtalk_mcp_url": "http://test.invalid/mcp",
        "dingtalk_mcp_auth_token": "test-token",
        "dingtalk_mcp_search_user_tool": "find_user",
        "dingtalk_mcp_send_text_tool": "send_text",
    }.items():
        monkeypatch.setattr(settings, name, value)

    client = DingTalkMCPClient(transport=server)
    capability = asyncio.run(client.describe_capabilities())
    assert capability["status"] == "ready"
    assert asyncio.run(client.resolve_user_by_mobile("19900001111"))["user_id"] == "ding-test-user"
    assert asyncio.run(client.send_text("ding-test-user", "测试通知"))["ok"] is True
    assert calls == [
        ("find", "19900001111"),
        ("send", "ding-test-user", "测试通知"),
    ]


def test_official_stdio_adapter_uses_robot_single_chat_schema(monkeypatch):
    from mcp.server import MCPServer

    calls = []
    server = MCPServer("official-dingtalk")

    @server.tool(name="batchSendMessageToUsersByRobot")
    def send_to_users(userIds: list[str], msgKey: str, msgParam: dict) -> dict:
        calls.append((userIds, msgKey, msgParam))
        return {"ok": True}

    for name, value in {
        "dingtalk_enabled": True,
        "dingtalk_mode": "live",
        "dingtalk_mcp_transport": "stdio",
        "dingtalk_mcp_search_user_tool": "getUserIdByMobile",
        "dingtalk_mcp_send_text_tool": "batchSendMessageToUsersByRobot",
    }.items():
        monkeypatch.setattr(settings, name, value)
    result = asyncio.run(DingTalkMCPClient(transport=server).send_text("zhuxingfu-id", "经营提醒正文"))
    assert result["ok"] is True
    assert calls == [(["zhuxingfu-id"], "sampleMarkdown", {"title": "经营提醒", "text": "经营提醒正文"})]


def test_live_send_is_fixed_to_configured_zhu_user(monkeypatch):
    import services.notifications as notices
    from database.models import NotificationDispatchOutbox

    plan = SimpleNamespace(
        plan_id="notice_live", analysis_run_id="run_1", session_id="session_1", store_id=1,
        intended_recipient="某门店店长", effective_recipient="朱兴福", message_text="GMV 1200 元\n1. 核查现场",
        status="pending_approval", version=1, idempotency_key="create-idempotency-key",
        dispatch_idempotency_key=None, result_detail=None, approved_by=None, cancelled_by=None,
        last_request_id=None, created_at=None, approved_at=None, dispatched_at=None,
    )
    sent = []

    class _Client:
        def send_text(self, user_id, text, idempotency_key):
            sent.append((user_id, text, idempotency_key))
            return {"ok": True}

    class _ScalarResult:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return self

        def first(self):
            return self.value

    class _Session:
        outbox = None

        def execute(self, statement):
            entity = statement.column_descriptions[0].get("entity")
            if entity is NotificationDispatchOutbox:
                return _ScalarResult(self.outbox)
            raise AssertionError(f"unexpected query: {statement}")

        def add(self, row):
            self.outbox = row

        def commit(self):
            return None

        def rollback(self):
            return None

    session = _Session()
    monkeypatch.setattr(notices, "get_notification_plan", lambda *_: plan)
    monkeypatch.setattr(notices, "_analysis_payload_for_plan", lambda *_: {"metrics": ["GMV 1200 元"]})
    monkeypatch.setattr(notices, "DingTalkDWSHostClient", _Client)
    monkeypatch.setattr(settings, "dingtalk_enabled", True)
    monkeypatch.setattr(settings, "dingtalk_mode", "live")
    monkeypatch.setattr(settings, "dingtalk_dispatcher", "dws_host")
    monkeypatch.setattr(settings, "dingtalk_live_recipient_name", "朱兴福")
    monkeypatch.setattr(settings, "dingtalk_live_recipient_user_id", "zhuxingfu-id")

    result = confirm_notification_plan("notice_live", session)
    assert result["status"] == "sent"
    assert plan.dispatch_idempotency_key == _dispatch_key(plan.plan_id)
    assert plan.dispatch_idempotency_key != plan.idempotency_key
    assert session.outbox.status == "sent"
    assert session.outbox.attempts == 1
    assert sent == [("zhuxingfu-id", plan.message_text, plan.dispatch_idempotency_key)]


def test_edit_uses_optimistic_version_and_rejects_stale_page(monkeypatch):
    import services.notifications as notices

    plan = SimpleNamespace(
        plan_id="notice_version", analysis_run_id="run_1", session_id="session_1", store_id=1,
        intended_recipient="店长", effective_recipient="测试人", message_text="GMV 1200 元",
        status="pending_approval", version=3, result_detail=None, approved_by=None,
        cancelled_by=None, last_request_id=None, created_at=None, approved_at=None, dispatched_at=None,
    )
    locked = []
    monkeypatch.setattr(
        notices, "get_notification_plan",
        lambda _plan_id, _session, for_update=False: locked.append(for_update) or plan,
    )
    monkeypatch.setattr(notices, "_analysis_payload_for_plan", lambda *_: {"metrics": ["GMV 1200 元"]})
    session = SimpleNamespace(commit=lambda: None)

    with pytest.raises(ValueError, match="当前版本 3"):
        update_notification_text(
            "notice_version", "GMV 1200 元", session, expected_version=2
        )
    assert locked == [True]
    assert plan.message_text == "GMV 1200 元"


def test_dws_host_client_rejects_unconfigured_or_mismatched_recipient(monkeypatch):
    client = DingTalkDWSHostClient()
    monkeypatch.setattr(settings, "dingtalk_enabled", True)
    monkeypatch.setattr(settings, "dingtalk_mode", "live")
    monkeypatch.setattr(settings, "dingtalk_live_recipient_user_id", "zhuxingfu-id")
    monkeypatch.setattr(settings, "collector_url", "http://host.docker.internal:8001")

    with pytest.raises(DingTalkDWSError, match="收件人不匹配"):
        client.send_text("someone-else", "测试", "idempotency-key")


def test_dws_host_client_explains_when_windows_collector_is_unreachable(monkeypatch):
    import integrations.dingtalk_dws as dws

    monkeypatch.setattr(settings, "dingtalk_enabled", True)
    monkeypatch.setattr(settings, "dingtalk_mode", "live")
    monkeypatch.setattr(settings, "dingtalk_live_recipient_user_id", "zhuxingfu-id")
    monkeypatch.setattr(settings, "collector_url", "http://host.docker.internal:8001")

    def _unreachable(*_args, **_kwargs):
        raise dws.httpx.ConnectError("network unreachable")

    monkeypatch.setattr(dws.httpx, "post", _unreachable)
    with pytest.raises(DingTalkDWSError, match="start_collector"):
        DingTalkDWSHostClient().send_text("zhuxingfu-id", "测试", "idempotency-key")
