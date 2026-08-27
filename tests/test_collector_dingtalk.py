"""DWS personal-chat command contract tests."""

from collector_main import _dws_send_text_args


def test_dws_personal_message_preserves_content_and_idempotency_flags():
    text = "【经营情况沟通】\n\n一、情况说明\n营业额较上周下降。"

    args = _dws_send_text_args("open-dingtalk-id", text, "notice_12345678")

    assert args == [
        "chat", "message", "send", "--open-dingtalk-id", "open-dingtalk-id",
        "--content", text, "--idempotency-key", "notice_12345678",
    ]
    assert "--text" not in args
    assert "--uuid" not in args
