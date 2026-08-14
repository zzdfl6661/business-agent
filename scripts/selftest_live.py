# -*- coding: utf-8 -*-
"""真实服务聊天链路自测：鉴权 + SSE + done 事件结构（含 pending_plans 字段）"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8011"
TOKEN = "test-token-123"

# 1. 无 token 聊天 → 401
try:
    urllib.request.urlopen(urllib.request.Request(
        BASE + "/api/chat/stream", method="POST",
        data=json.dumps({"question": "hi", "session_id": "s_test"}).encode(),
        headers={"Content-Type": "application/json"}))
    print("FAIL: 无 token 应 401")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"OK: 无 token → {e.code}")

# 2. 有 token 聊天（kb 问题，验证 SSE 事件流 + done 结构）
req = urllib.request.Request(
    BASE + "/api/chat/stream", method="POST",
    data=json.dumps({"question": "门店晋升需要什么条件", "session_id": "s_test_auth"}).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
events = []
with urllib.request.urlopen(req, timeout=120) as resp:
    buf = b""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            ev, data = "message", ""
            for line in block.decode("utf-8", "replace").split("\n"):
                if line.startswith("event: "):
                    ev = line[7:].strip()
                elif line.startswith("data: "):
                    data += line[6:]
            events.append((ev, data))

names = [e[0] for e in events]
print("SSE 事件序列:", names)
assert "progress" in names and "done" in names, "缺少 progress/done 事件"
done = json.loads([d for ev, d in events if ev == "done"][0])
print("done 键:", sorted(done.keys()))
assert "pending_plans" in done, "done 事件缺少 pending_plans"
assert done["pending_plans"] == [], "kb 问题不应有执行计划"
assert done.get("user_question") == "门店晋升需要什么条件"
print("OK: done 事件结构正确（pending_plans=[]，kb 链路）")

# 3. 会话持久化未被鉴权影响
req = urllib.request.Request(
    BASE + "/api/sessions/s_test_auth/messages",
    headers={"Authorization": "Bearer " + TOKEN})
with urllib.request.urlopen(req, timeout=30) as resp:
    d = json.loads(resp.read().decode("utf-8"))
print("OK: 会话消息接口正常, message_count =", d.get("message_count"))
print("ALL LIVE TESTS PASSED")
