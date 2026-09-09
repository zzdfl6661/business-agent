import asyncio

from config.auth import ApiTokenMiddleware
from config.request_id import get_operator_id, get_request_id


def test_auth_middleware_propagates_request_and_operator_context():
    observed = {}
    messages = []

    async def app(scope, _receive, send):
        observed["request_id"] = get_request_id()
        observed["operator_id"] = get_operator_id()
        observed["state"] = dict(scope["state"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "path": "/api/notifications",
        "headers": [
            (b"authorization", b"Bearer secret"),
            (b"x-request-id", b"req-interview-001"),
            (b"x-operator-id", b"operator-42"),
        ],
    }
    asyncio.run(ApiTokenMiddleware(app, token="secret")(scope, receive, send))

    assert observed == {
        "request_id": "req-interview-001",
        "operator_id": "operator-42",
        "state": {"request_id": "req-interview-001", "operator_id": "operator-42"},
    }
    response_headers = dict(messages[0]["headers"])
    assert response_headers[b"x-request-id"] == b"req-interview-001"
    assert get_request_id() == ""
    assert get_operator_id() == ""
