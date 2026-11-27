"""Agentsii local bridge: lets Codex use providers that only speak OpenAI
Chat Completions.

Current Codex only talks the Responses API. For a Chat-Completions-only
provider, Codex is pointed at  http://127.0.0.1:<port>/p/<provider_id>/v1  and
this bridge translates each streamed Responses request into a streamed Chat
Completions request to the real provider, and the answer back into Responses
SSE events (text, function tools, freeform/custom tools, usage).

* stdlib only (runs inside the small agentsii-auth.exe)
* listens on 127.0.0.1 only
* the API key is not stored here: Codex sends it (obtained from Windows
  Credential Manager through agentsii-auth.exe) and the bridge forwards it
* runs inside the Agentsii background service (tray), which starts with
  Windows; agentsii-auth.exe starts the service if Codex needs it
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HEALTH_PATH = "/agentsii-bridge/health"
_LAST_ACTIVITY = time.time()


# ------------------------------------------------------------------ registry

def _data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Agentsii"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "agentsii"


def registry_path() -> Path:
    return _data_dir() / "bridge.json"


def load_registry() -> dict:
    p = registry_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("providers", {})
            return data
    except (OSError, ValueError):
        pass
    return {"port": 0, "providers": {}}


def save_registry(data: dict) -> None:
    p = registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def is_bridge_running(port: int, timeout: float = 1.5) -> bool:
    """True only for an Agentsii bridge serving THIS user's registry."""
    if not port:
        return False
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", HEALTH_PATH)
        r = c.getresponse()
        body = r.read()
        c.close()
        if r.status != 200 or b"agentsii" not in body:
            return False
        reg = json.loads(body).get("registry")
        return reg is None or os.path.normcase(reg) == os.path.normcase(str(registry_path()))
    except (OSError, ValueError):
        return False


def _pick_port(current: int) -> int:
    if current and (_port_free(current) or is_bridge_running(current)):
        return current
    for cand in (17821, 17822, 17823, 27821, 37821):
        if _port_free(cand) or is_bridge_running(cand):
            return cand
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_port() -> tuple[int, dict]:
    data = load_registry()
    previous = json.loads(json.dumps(data))
    port = _pick_port(int(data.get("port") or 0))
    if port != data.get("port"):
        data["port"] = port
        save_registry(data)
    return port, previous


def register_provider(provider_id: str, upstream: str, models: list[str] | None = None,
                      bridged: bool = True, service_cmd: list[str] | None = None,
                      name: str = "", context: dict | None = None) -> tuple[int, dict]:
    """Add/replace a provider; returns (port, previous_registry) for rollback."""
    data = load_registry()
    previous = json.loads(json.dumps(data))
    port = _pick_port(int(data.get("port") or 0))
    data["port"] = port
    if service_cmd:
        data["service_cmd"] = service_cmd
    data["providers"][provider_id] = {"upstream": upstream.rstrip("/"), "bridged": bool(bridged),
                                      "models": list(models or []), "name": name or provider_id,
                                      "context": dict(context or {})}
    save_registry(data)
    return port, previous


def running_pid() -> int | None:
    port = int(load_registry().get("port") or 0)
    if not port:
        return None
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", HEALTH_PATH)
        r = c.getresponse()
        data = json.loads(r.read())
        c.close()
        return int(data.get("pid"))
    except (OSError, ValueError, TypeError):
        return None


def local_base_url(port: int, provider_id: str) -> str:
    return f"http://127.0.0.1:{port}/p/{provider_id}/v1"


# ------------------------------------------------------------------ request translation

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    out.append(t)
            elif isinstance(p, str):
                out.append(p)
        return "\n".join(out)
    if content is None:
        return ""
    return json.dumps(content)


def _message_content(content):
    if isinstance(content, str):
        return content
    parts, has_image = [], False
    for p in content or []:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t in ("input_text", "output_text", "text", "summary_text"):
            parts.append({"type": "text", "text": p.get("text", "")})
        elif t == "input_image":
            url = p.get("image_url") or (p.get("image") or {}).get("url")
            if url:
                has_image = True
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if not has_image:
        return "\n".join(p["text"] for p in parts)
    return parts


CUSTOM_ARG = "input"


def responses_to_chat(body: dict) -> tuple[dict, dict]:
    """Translate a Responses API request body to a Chat Completions body.
    Returns (chat_body, tool_kinds) where tool_kinds maps tool name -> kind."""
    messages: list[dict] = []
    instr = body.get("instructions")
    if instr:
        messages.append({"role": "system", "content": _text_of(instr)})

    kinds: dict[str, str] = {}
    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        tt = t.get("type")
        if tt == "function":
            name = t.get("name") or (t.get("function") or {}).get("name")
            if not name:
                continue
            params = t.get("parameters") or (t.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}
            tools.append({"type": "function", "function": {
                "name": name, "description": t.get("description", ""), "parameters": params}})
            kinds[name] = "function"
        elif tt == "custom":
            name = t.get("name")
            if not name:
                continue
            desc = t.get("description", "")
            fmt = t.get("format") or {}
            if isinstance(fmt, dict) and fmt.get("definition"):
                desc += "\n\nThe `input` argument must follow this grammar:\n" + str(fmt["definition"])[:6000]
            tools.append({"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": {CUSTOM_ARG: {"type": "string",
                               "description": "Raw tool input (freeform text)."}},
                               "required": [CUSTOM_ARG], "additionalProperties": False}}})
            kinds[name] = "custom"
        elif tt == "local_shell":
            tools.append({"type": "function", "function": {
                "name": "local_shell", "description": "Run a shell command.",
                "parameters": {"type": "object", "properties": {
                    "command": {"type": "array", "items": {"type": "string"}},
                    "workdir": {"type": "string"}, "timeout_ms": {"type": "number"}},
                    "required": ["command"]}}})
            kinds["local_shell"] = "local_shell"
        # hosted tools (web_search, file_search, ...) cannot be bridged: dropped

    def last_assistant_with_calls():
        if messages and messages[-1]["role"] == "assistant" and "tool_calls" in messages[-1]:
            return messages[-1]
        return None

    for item in body.get("input") or []:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        it = item.get("type") or ("message" if "role" in item else None)
        if it == "message":
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            if role not in ("system", "user", "assistant"):
                role = "user"
            content = _message_content(item.get("content"))
            if role == "system" and not isinstance(content, str):
                content = _text_of(item.get("content"))
            messages.append({"role": role, "content": content})
        elif it in ("function_call", "custom_tool_call", "local_shell_call"):
            call_id = item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex[:12])
            if it == "function_call":
                name, args = item.get("name", ""), item.get("arguments") or "{}"
            elif it == "custom_tool_call":
                name, args = item.get("name", ""), json.dumps({CUSTOM_ARG: item.get("input", "")})
            else:
                action = item.get("action") or {}
                name = "local_shell"
                args = json.dumps({"command": action.get("command", []),
                                   "workdir": action.get("working_directory"),
                                   "timeout_ms": action.get("timeout_ms")})
            tc = {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}
            prev = last_assistant_with_calls()
            if prev is not None:
                prev["tool_calls"].append(tc)
            elif messages and messages[-1]["role"] == "assistant" and isinstance(messages[-1].get("content"), str):
                messages[-1]["tool_calls"] = [tc]
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [tc]})
        elif it in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            out = item.get("output")
            if isinstance(out, dict):
                out = out.get("content") if "content" in out else json.dumps(out)
            messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": _text_of(out)})
        # reasoning / web_search_call / other items are not representable: skipped

    chat: dict = {"model": body.get("model"), "messages": messages, "stream": bool(body.get("stream", True))}
    if tools:
        chat["tools"] = tools
        tc = body.get("tool_choice")
        if tc in ("auto", "required", "none"):
            chat["tool_choice"] = tc
        if body.get("parallel_tool_calls") is not None:
            chat["parallel_tool_calls"] = bool(body.get("parallel_tool_calls"))
    for k_from, k_to in (("max_output_tokens", "max_tokens"), ("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(k_from) is not None:
            chat[k_to] = body[k_from]
    if chat["stream"]:
        chat["stream_options"] = {"include_usage": True}
    return chat, kinds


# ------------------------------------------------------------------ response translation

class StreamTranslator:
    """Turns Chat Completions stream chunks into Responses API SSE events."""

    def __init__(self, model: str, kinds: dict[str, str]):
        self.model = model
        self.kinds = kinds
        self.rid = "resp_" + uuid.uuid4().hex[:24]
        self.mid = "msg_" + uuid.uuid4().hex[:24]
        self.created = int(time.time())
        self.seq = 0
        self.text = ""
        self.text_started = False
        self.calls: dict[int, dict] = {}
        self.usage = None
        self.finish_reason = None
        self.output_items: list[dict] = []

    def _ev(self, obj: dict) -> dict:
        obj["sequence_number"] = self.seq
        self.seq += 1
        return obj

    def _resp(self, status: str, output=None) -> dict:
        return {"id": self.rid, "object": "response", "created_at": self.created, "model": self.model,
                "status": status, "output": output or []}

    def start(self) -> list[dict]:
        return [self._ev({"type": "response.created", "response": self._resp("in_progress")}),
                self._ev({"type": "response.in_progress", "response": self._resp("in_progress")})]

    def feed(self, chunk: dict) -> list[dict]:
        evs: list[dict] = []
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or ch.get("message") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                if not self.text_started:
                    self.text_started = True
                    evs.append(self._ev({"type": "response.output_item.added", "output_index": 0, "item": {
                        "type": "message", "id": self.mid, "role": "assistant", "status": "in_progress", "content": []}}))
                    evs.append(self._ev({"type": "response.content_part.added", "item_id": self.mid,
                                         "output_index": 0, "content_index": 0,
                                         "part": {"type": "output_text", "text": "", "annotations": []}}))
                self.text += content
                evs.append(self._ev({"type": "response.output_text.delta", "item_id": self.mid,
                                     "output_index": 0, "content_index": 0, "delta": content}))
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", len(self.calls))
                cur = self.calls.setdefault(idx, {"id": None, "name": "", "args": ""})
                if tc.get("id"):
                    cur["id"] = tc["id"]
                fn = tc.get("function") or {}
                name = fn.get("name")
                if name and name != cur["name"]:
                    cur["name"] = name if not cur["name"] or name.startswith(cur["name"]) else cur["name"] + name
                if fn.get("arguments"):
                    cur["args"] += fn["arguments"]
            if ch.get("finish_reason"):
                self.finish_reason = ch["finish_reason"]
        return evs

    def _call_item(self, c: dict) -> dict:
        call_id = c["id"] or ("call_" + uuid.uuid4().hex[:20])
        name = c["name"]
        kind = self.kinds.get(name, "function")
        if kind == "custom":
            try:
                inp = json.loads(c["args"] or "{}").get(CUSTOM_ARG, "")
            except (ValueError, AttributeError):
                inp = c["args"]
            return {"type": "custom_tool_call", "id": "ctc_" + uuid.uuid4().hex[:20], "call_id": call_id,
                    "name": name, "input": inp if isinstance(inp, str) else json.dumps(inp), "status": "completed"}
        if kind == "local_shell":
            try:
                a = json.loads(c["args"] or "{}")
            except ValueError:
                a = {}
            return {"type": "local_shell_call", "id": "lsh_" + uuid.uuid4().hex[:20], "call_id": call_id,
                    "status": "completed", "action": {"type": "exec", "command": a.get("command") or [],
                                                      "working_directory": a.get("workdir"),
                                                      "timeout_ms": a.get("timeout_ms")}}
        return {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:20], "call_id": call_id,
                "name": name, "arguments": c["args"] or "{}", "status": "completed"}

    def finish(self) -> list[dict]:
        evs: list[dict] = []
        out_index = 0
        if self.text_started:
            part = {"type": "output_text", "text": self.text, "annotations": []}
            evs.append(self._ev({"type": "response.output_text.done", "item_id": self.mid, "output_index": 0,
                                 "content_index": 0, "text": self.text}))
            evs.append(self._ev({"type": "response.content_part.done", "item_id": self.mid, "output_index": 0,
                                 "content_index": 0, "part": part}))
            item = {"type": "message", "id": self.mid, "role": "assistant", "status": "completed", "content": [part]}
            evs.append(self._ev({"type": "response.output_item.done", "output_index": 0, "item": item}))
            self.output_items.append(item)
            out_index = 1
        for _, c in sorted(self.calls.items()):
            if not c["name"]:
                continue
            item = self._call_item(c)
            evs.append(self._ev({"type": "response.output_item.added", "output_index": out_index, "item": item}))
            evs.append(self._ev({"type": "response.output_item.done", "output_index": out_index, "item": item}))
            self.output_items.append(item)
            out_index += 1
        u = self.usage or {}
        pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        reasoning = int(((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
        usage = {"input_tokens": pt, "input_tokens_details": {"cached_tokens": cached},
                 "output_tokens": ct, "output_tokens_details": {"reasoning_tokens": reasoning},
                 "total_tokens": int(u.get("total_tokens") or pt + ct)}
        status = "incomplete" if self.finish_reason == "length" and not self.calls else "completed"
        resp = {**self._resp(status, self.output_items), "usage": usage}
        if status == "incomplete":
            resp["incomplete_details"] = {"reason": "max_output_tokens"}
            evs.append(self._ev({"type": "response.incomplete", "response": resp}))
        else:
            evs.append(self._ev({"type": "response.completed", "response": resp}))
        self.final_response = resp
        return evs


# ------------------------------------------------------------------ upstream

def _upstream_conn(upstream: str, timeout: float = 600):
    u = urlsplit(upstream)
    if u.scheme == "https":
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                           context=ssl.create_default_context()), u.path.rstrip("/")
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout), u.path.rstrip("/")


def _iter_sse(resp):
    buf = b""
    while True:
        chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                yield line[5:].strip().decode("utf-8", errors="replace")
    if buf.strip().startswith(b"data:"):
        yield buf.strip()[5:].strip().decode("utf-8", errors="replace")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AgentsiiBridge/1.0"

    def log_message(self, fmt, *args):  # never log headers / keys
        pass

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self):
        global _LAST_ACTIVITY
        _LAST_ACTIVITY = time.time()
        path = self.path.split("?")[0]
        if path == HEALTH_PATH:
            return None, "health"
        parts = path.split("/")
        # /p/<id>/v1/<endpoint...>
        if len(parts) >= 5 and parts[1] == "p":
            pid = parts[2]
            rest = "/" + "/".join(parts[4:])
            prov = load_registry()["providers"].get(pid)
            if not prov or not prov.get("bridged", True):
                return None, "unknown"
            return prov["upstream"], rest
        return None, "notfound"

    def do_GET(self):
        upstream, rest = self._route()
        if rest == "health":
            return self._json(200, {"agentsii": True, "pid": os.getpid(), "registry": str(registry_path())})
        if upstream is None:
            return self._json(404, {"error": {"message": f"agentsii bridge: {rest}"}})
        if rest != "/models":
            return self._json(404, {"error": {"message": "not supported by agentsii bridge"}})
        conn, base = _upstream_conn(upstream, 60)
        try:
            conn.request("GET", base + "/models", headers=self._fwd_headers())
            r = conn.getresponse()
            data = r.read()
        except OSError as e:
            return self._json(502, {"error": {"message": f"upstream unreachable: {e}"}})
        finally:
            conn.close()
        self.send_response(r.status)
        self.send_header("Content-Type", r.getheader("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _fwd_headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": "Agentsii-Bridge/1.0"}
        auth = self.headers.get("Authorization")
        if auth:
            h["Authorization"] = auth
        return h

    def do_POST(self):
        upstream, rest = self._route()
        if upstream is None:
            return self._json(404, {"error": {"message": f"agentsii bridge: {rest}"}})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if self.headers.get("Content-Encoding", "").lower() == "zstd":
            return self._json(415, {"error": {"message": "zstd request bodies are not supported"}})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": {"message": "invalid JSON"}})
        if rest != "/responses":
            return self._json(404, {"error": {"message": f"{rest} is not supported by the agentsii bridge"}})
        chat, kinds = responses_to_chat(body)
        stream = chat["stream"]
        r, conn = self._post_chat(upstream, chat)
        if r is None:
            return
        if r.status == 400 and "stream_options" in chat:
            err = r.read()
            conn.close()
            if b"stream_options" in err or b"include_usage" in err:
                chat.pop("stream_options", None)
                r, conn = self._post_chat(upstream, chat)
                if r is None:
                    return
            else:
                return self._passthrough_error(400, err)
        if r.status != 200:
            data = r.read()
            conn.close()
            return self._passthrough_error(r.status, data)
        tr = StreamTranslator(body.get("model") or chat.get("model") or "", kinds)
        try:
            if stream and "event-stream" in (r.getheader("Content-Type") or ""):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                for ev in tr.start():
                    self._sse(ev)
                for data in _iter_sse(r):
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(chunk, dict) and chunk.get("error"):
                        err = chunk["error"]
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        self._sse(tr._ev({"type": "response.failed", "response": {
                            **tr._resp("failed"), "error": {"code": "upstream_error", "message": msg}}}))
                        return
                    for ev in tr.feed(chunk):
                        self._sse(ev)
                for ev in tr.finish():
                    self._sse(ev)
            else:
                data = r.read()
                try:
                    obj = json.loads(data)
                except ValueError:
                    return self._json(502, {"error": {"message": "upstream returned invalid JSON"}})
                tr.feed(obj)
                evs = tr.start() + tr.finish()
                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    for ev in evs:
                        self._sse(ev)
                else:
                    self._json(200, tr.final_response)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    def _post_chat(self, upstream, chat):
        conn, base = _upstream_conn(upstream)
        try:
            conn.request("POST", base + "/chat/completions", body=json.dumps(chat).encode(),
                         headers=self._fwd_headers())
            return conn.getresponse(), conn
        except OSError as e:
            conn.close()
            self._json(502, {"error": {"message": f"upstream unreachable: {e}"}})
            return None, None

    def _passthrough_error(self, status, data: bytes):
        try:
            obj = json.loads(data)
            if not isinstance(obj, dict) or "error" not in obj:
                obj = {"error": {"message": data.decode("utf-8", "replace")[:500]}}
        except ValueError:
            obj = {"error": {"message": data.decode("utf-8", "replace")[:500] or f"HTTP {status}"}}
        self._json(status, obj)

    def _sse(self, ev: dict):
        self.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())
        self.wfile.flush()


def make_server(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv


def serve(port: int | None = None, idle_exit: float | None = None) -> int:
    """Run the bridge without UI (used by tests / --bridge)."""
    port = port or int(load_registry().get("port") or 0)
    if not port:
        return 2
    try:
        srv = make_server(port)
    except OSError:
        return 0 if is_bridge_running(port) else 3
    if idle_exit:
        def watchdog():
            while True:
                time.sleep(30)
                if time.time() - _LAST_ACTIVITY > idle_exit:
                    srv.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
    srv.serve_forever()
    return 0


def start_service_from_helper(wait: float = 30.0) -> bool:
    """Called by agentsii-auth when Codex needs a bridged provider and the
    Agentsii background service is not running. Uses ShellExecute on Windows so
    the service never inherits Codex's pipes."""
    import subprocess
    reg = load_registry()
    port = int(reg.get("port") or 0)
    if not port:
        return False
    if is_bridge_running(port):
        return True
    cmd = reg.get("service_cmd") or []
    if not cmd:
        return False
    if sys.platform == "win32" and cmd[0].lower().endswith(".exe"):
        os.startfile(cmd[0], arguments=subprocess.list2cmdline(cmd[1:]), show_cmd=0)  # type: ignore[call-arg]
    else:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=(sys.platform != "win32"))
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_bridge_running(port, timeout=0.5):
            return True
        time.sleep(0.25)
    return False
