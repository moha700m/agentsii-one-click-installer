import json
import tomllib

import httpx

from agentsii import bridge, credentials
from agentsii.bridge import StreamTranslator, responses_to_chat
from mock_provider import MockProvider
from test_pipeline import KEY, run

SHELL_TOOL = {"type": "function", "name": "shell", "description": "run",
              "parameters": {"type": "object", "properties": {"command": {"type": "array", "items": {"type": "string"}}},
                             "required": ["command"]}}
PATCH_TOOL = {"type": "custom", "name": "apply_patch", "description": "patch files",
              "format": {"type": "grammar", "syntax": "lark", "definition": "start: 'x'"}}


def test_request_translation():
    body = {"model": "m", "instructions": "SYS", "stream": True, "tools": [SHELL_TOOL, PATCH_TOOL, {"type": "web_search"}],
            "tool_choice": "auto", "parallel_tool_calls": False,
            "input": [
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "DEV"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "reasoning", "summary": []},
                {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{\"command\":[\"ls\"]}"},
                {"type": "custom_tool_call", "call_id": "c2", "name": "apply_patch", "input": "*** Begin Patch"},
                {"type": "function_call_output", "call_id": "c1", "output": "file.txt"},
                {"type": "custom_tool_call_output", "call_id": "c2", "output": "Done"},
            ]}
    chat, kinds = responses_to_chat(body)
    assert kinds == {"shell": "function", "apply_patch": "custom"}
    assert [t["function"]["name"] for t in chat["tools"]] == ["shell", "apply_patch"]
    roles = [m["role"] for m in chat["messages"]]
    assert roles == ["system", "system", "user", "assistant", "tool", "tool"]
    calls = chat["messages"][3]["tool_calls"]
    assert [c["id"] for c in calls] == ["c1", "c2"]
    assert json.loads(calls[1]["function"]["arguments"]) == {"input": "*** Begin Patch"}
    assert chat["messages"][4] == {"role": "tool", "tool_call_id": "c1", "content": "file.txt"}
    assert chat["stream_options"] == {"include_usage": True}


def test_stream_translation_text_and_tools():
    tr = StreamTranslator("m", {"apply_patch": "custom", "shell": "function"})
    evs = tr.start()
    evs += tr.feed({"choices": [{"delta": {"content": "Hel"}}]})
    evs += tr.feed({"choices": [{"delta": {"content": "lo"}}]})
    evs += tr.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "apply_patch", "arguments": "{\"in"}}]}}]})
    evs += tr.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "put\":\"P\"}"}}]}}]})
    evs += tr.feed({"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "b", "function": {"name": "shell", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]})
    evs += tr.feed({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
    evs += tr.finish()
    types = [e["type"] for e in evs]
    assert types[0] == "response.created" and types[-1] == "response.completed"
    assert "".join(e["delta"] for e in evs if e["type"] == "response.output_text.delta") == "Hello"
    done = [e["item"] for e in evs if e["type"] == "response.output_item.done"]
    assert done[0]["type"] == "message" and done[0]["content"][0]["text"] == "Hello"
    assert done[1] == {**done[1], "type": "custom_tool_call", "call_id": "a", "name": "apply_patch", "input": "P"}
    assert done[2]["type"] == "function_call" and done[2]["call_id"] == "b"
    assert evs[-1]["response"]["usage"]["input_tokens"] == 5
    assert [e["sequence_number"] for e in evs] == list(range(len(evs)))


def test_chat_only_provider_connects_through_bridge(isolated):
    with MockProvider(key=KEY, responses_enabled=False) as mp:
        res = run(mp.url + "/v1", isolated)
        assert res.ok, (res.error_code, res.error_detail)
        assert res.bridged and not res.supports_responses and res.supports_chat
        assert sorted(res.added) == ["agent-codex-1", "agent-pro-2", "chat-only-3"]
        data = tomllib.loads((isolated / "config.toml").read_text())
        prov = data["model_providers"]["my-gateway"]
        port = bridge.load_registry()["port"]
        assert prov["base_url"] == f"http://127.0.0.1:{port}/p/my-gateway/v1"
        assert prov["wire_api"] == "responses" and prov["auth"]["refresh_interval_ms"] == 300000
        assert bridge.load_registry()["providers"]["my-gateway"]["upstream"] == mp.url + "/v1"
        assert bridge.is_bridge_running(port)  # started by agentsii-auth during verification

        # Codex-like tool loop through the bridge
        url = prov["base_url"] + "/responses"
        hdr = {"Authorization": f"Bearer {credentials.get_key('my-gateway')}"}
        body = {"model": "agent-codex-1", "instructions": "x", "stream": True, "tools": [SHELL_TOOL, PATCH_TOOL],
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "RUN_TOOL"}]}]}

        def events(b):
            with httpx.stream("POST", url, json=b, headers=hdr, timeout=30) as r:
                assert r.status_code == 200
                return [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]

        ev1 = events(body)
        call = next(e["item"] for e in ev1 if e["type"] == "response.output_item.done")
        assert call["type"] == "function_call" and call["name"] == "shell"
        assert "AGENTSII_TOOL_OK" in json.loads(call["arguments"])["command"][-1]
        body["input"] += [call, {"type": "function_call_output", "call_id": call["call_id"], "output": "AGENTSII_TOOL_OK\n"}]
        ev2 = events(body)
        assert ev2[-1]["type"] == "response.completed"
        text = "".join(e.get("delta", "") for e in ev2 if e["type"] == "response.output_text.delta")
        assert text == "AGENTSII_E2E_OK"
        assert any(r.get("tool_result_ok") for r in mp.record)
        # wrong key is passed through as 401
        r = httpx.post(url, json=body, headers={"Authorization": "Bearer nope"}, timeout=30)
        assert r.status_code == 401


def test_bridge_registry_rolled_back_on_failure(isolated, monkeypatch):
    def boom(pid, key):
        raise credentials.CredentialError("vault locked")
    monkeypatch.setattr(credentials, "store_key", boom)
    with MockProvider(key=KEY, responses_enabled=False) as mp:
        res = run(mp.url + "/v1", isolated)
    assert not res.ok and res.error_code == "credential_failed"
    assert "my-gateway" not in bridge.load_registry()["providers"]
