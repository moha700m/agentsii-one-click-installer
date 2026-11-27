"""A small OpenAI-compatible provider used by tests and by the Windows CI
end-to-end run (real Codex CLI talks to it through the configured provider).

Models:
  agent-codex-1  -> full streamed Responses API (compatible)
  agent-pro-2    -> full streamed Responses API (compatible)
  chat-only-3    -> Responses returns 400 (only chat completions)
  broken-4       -> stream emits response.failed
  text-embedding-3-small -> filtered out as non-agent model
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["agent-codex-1", "agent-pro-2", "chat-only-3", "broken-4", "text-embedding-3-small"]


def make_handler(key: str, responses_enabled: bool, record: list, models_path: str = "/v1/models",
                 reply_text: str = "AGENTSII_E2E_OK"):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _auth_ok(self):
            return self.headers.get("Authorization", "") == f"Bearer {key}"

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            path = self.path.split("?")[0]
            record.append({"method": "GET", "path": self.path, "auth_ok": self._auth_ok()})
            if path == models_path:
                if not self._auth_ok():
                    return self._json(401, {"error": {"message": "Invalid API key"}})
                return self._json(200, {"object": "list", "data": [
                    {"id": m, "object": "model", "owned_by": "mock"} for m in MODELS]})
            if path in ("/", "/v1"):
                return self._json(200, {"ok": True})
            return self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._body()
            tool_msgs = [m for m in body.get("messages") or [] if isinstance(m, dict) and m.get("role") == "tool"]
            record.append({"method": "POST", "path": path, "model": body.get("model"),
                           "auth_ok": self._auth_ok(), "stream": body.get("stream"),
                           "has_tools": bool(body.get("tools")),
                           "tool_names": [((t.get("function") or {}).get("name") or t.get("name")) for t in body.get("tools") or []][:40],
                           "tool_result_ok": any("AGENTSII_TOOL_OK" in str(m.get("content")) for m in tool_msgs)})
            if not self._auth_ok():
                return self._json(401, {"error": {"message": "Invalid API key"}})
            if path == "/v1/chat/completions":
                model = body.get("model")
                if model not in MODELS:
                    return self._json(404, {"error": {"message": f"model {model} not found"}})
                if model == "broken-4":
                    return self._json(500, {"error": {"message": "upstream exploded"}})
                if not body.get("stream"):
                    return self._json(200, {"id": "c1", "object": "chat.completion", "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "p"}, "finish_reason": "length"}]})
                return self._chat_stream(body)
            if path == "/v1/responses":
                if not responses_enabled:
                    return self._json(404, {"error": {"message": "Not Found"}})
                model = body.get("model")
                if model not in MODELS:
                    return self._json(404, {"error": {"message": f"model {model} not found"}})
                if model == "chat-only-3":
                    return self._json(400, {"error": {"message": "model does not support responses"}})
                return self._stream(model, failed=(model == "broken-4"))
            return self._json(404, {"error": {"message": "not found"}})

        def _chat_stream(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            model = body.get("model")

            def chunk(delta, finish=None, usage=None):
                obj = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": model,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usage is not None:
                    obj = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": model,
                           "choices": [], "usage": usage}
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()

            msgs = body.get("messages") or []
            has_tool_result = any(isinstance(m, dict) and m.get("role") == "tool" for m in msgs)
            shell = None
            for t in body.get("tools") or []:
                fn = t.get("function") or {}
                if "shell" in (fn.get("name") or "") and fn.get("name") != "local_shell":
                    shell = fn
                    break
            last_user = next((m for m in reversed(msgs) if isinstance(m, dict) and m.get("role") == "user"), {})
            wants_tool = "RUN_TOOL" in str(last_user.get("content"))
            if shell and wants_tool and not has_tool_result:
                props = (shell.get("parameters") or {}).get("properties") or {}
                ctype = (props.get("command") or {}).get("type")
                if ctype == "array":
                    cmd = ["cmd.exe", "/c", "echo AGENTSII_TOOL_OK"] if sys.platform == "win32" else ["echo", "AGENTSII_TOOL_OK"]
                else:
                    cmd = "echo AGENTSII_TOOL_OK"
                args = json.dumps({"command": cmd})
                chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_mock1", "type": "function",
                                                            "function": {"name": shell["name"], "arguments": ""}}]})
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[:10]}}]})
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[10:]}}]})
                chunk({}, "tool_calls")
            else:
                chunk({"role": "assistant", "content": ""})
                chunk({"content": reply_text[:8]})
                chunk({"content": reply_text[8:]})
                chunk({}, "stop")
            if (body.get("stream_options") or {}).get("include_usage"):
                chunk(None, usage={"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True

        def _stream(self, model, failed=False):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            rid = "resp_" + uuid.uuid4().hex[:12]
            mid = "msg_" + uuid.uuid4().hex[:12]

            def ev(obj):
                data = f"event: {obj['type']}\ndata: {json.dumps(obj)}\n\n".encode()
                self.wfile.write(data)
                self.wfile.flush()

            base = {"id": rid, "object": "response", "created_at": int(time.time()), "model": model,
                    "status": "in_progress", "output": []}
            ev({"type": "response.created", "sequence_number": 0, "response": base})
            if failed:
                ev({"type": "response.failed", "sequence_number": 1,
                    "response": {**base, "status": "failed", "error": {"code": "server_error", "message": "boom"}}})
                self.close_connection = True
                return
            item = {"type": "message", "id": mid, "role": "assistant", "status": "in_progress", "content": []}
            ev({"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": item})
            ev({"type": "response.content_part.added", "sequence_number": 2, "item_id": mid, "output_index": 0,
                "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
            ev({"type": "response.output_text.delta", "sequence_number": 3, "item_id": mid, "output_index": 0,
                "content_index": 0, "delta": reply_text})
            ev({"type": "response.output_text.done", "sequence_number": 4, "item_id": mid, "output_index": 0,
                "content_index": 0, "text": reply_text})
            part = {"type": "output_text", "text": reply_text, "annotations": []}
            ev({"type": "response.content_part.done", "sequence_number": 5, "item_id": mid, "output_index": 0,
                "content_index": 0, "part": part})
            done_item = {**item, "status": "completed", "content": [part]}
            ev({"type": "response.output_item.done", "sequence_number": 6, "output_index": 0, "item": done_item})
            usage = {"input_tokens": 12, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 3,
                     "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 15}
            ev({"type": "response.completed", "sequence_number": 7,
                "response": {**base, "status": "completed", "output": [done_item], "usage": usage}})
            self.close_connection = True

    return H


class MockProvider:
    def __init__(self, key="sk-test-123456789", responses_enabled=True, port=0, models_path="/v1/models"):
        self.key = key
        self.record: list = []
        self.server = ThreadingHTTPServer(("127.0.0.1", port),
                                          make_handler(key, responses_enabled, self.record, models_path))
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--key", required=True)
    ap.add_argument("--record", help="write request log (JSON lines) here")
    ap.add_argument("--no-responses", action="store_true", help="behave like a Chat-Completions-only provider")
    args = ap.parse_args()
    mp = MockProvider(key=args.key, port=args.port, responses_enabled=not args.no_responses)
    print(f"mock provider on {mp.url}", flush=True)
    try:
        if args.record:
            def dump():
                while True:
                    time.sleep(1)
                    with open(args.record, "w", encoding="utf-8") as f:
                        for r in list(mp.record):
                            f.write(json.dumps(r) + "\n")
            threading.Thread(target=dump, daemon=True).start()
        mp.server.serve_forever()
    except KeyboardInterrupt:
        pass
