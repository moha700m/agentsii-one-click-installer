"""Talks to the API provider directly: URL checks, key check, model discovery,
protocol detection and a per-model Codex compatibility test.

Current Codex only speaks the OpenAI Responses API (`wire_api = "responses"` is
the only supported value), streams over SSE and always sends function tools.
A model is only marked compatible when a real streamed Responses request that
carries a function tool completes successfully against the provider.
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import USER_AGENT
from .logutil import log

MAX_MODELS_TO_TEST = 60
TEST_CONCURRENCY = 6
CONNECT_TIMEOUT = 15.0
TEST_TIMEOUT = 90.0

# Names that are clearly not chat/agent models (embeddings, audio, images...).
_NON_AGENT = re.compile(
    r"(embed|embedding|whisper|tts|text-to-speech|dall-e|dalle|gpt-image|image-gen|"
    r"stable-diffusion|sdxl|flux|moderation|rerank|transcribe|speech|audio|realtime|"
    r"omni-moderation|davinci-002|babbage|clip|vision-encoder|guard)",
    re.IGNORECASE,
)


class ProviderError(Exception):
    """Error with a user-facing reason code + detail."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class ModelResult:
    id: str
    compatible: bool = False
    tested: bool = False
    reason: str = ""
    http_status: int = 0


@dataclass
class ProbeReport:
    base_url: str = ""
    models_url: str = ""
    key_verified: bool = False
    discovered: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    candidates: list[str] = field(default_factory=list)
    supports_responses: bool = False
    supports_chat: bool = False
    results: list[ModelResult] = field(default_factory=list)

    @property
    def compatible(self) -> list[str]:
        return [r.id for r in self.results if r.compatible]


# ---------------------------------------------------------------- URL checks

def _is_local_host(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


def normalize_base_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise ProviderError("url_empty")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ProviderError("url_scheme", parts.scheme)
    host = parts.hostname or ""
    if not host or " " in url:
        raise ProviderError("url_invalid", raw)
    if parts.username or parts.password:
        raise ProviderError("url_credentials")
    if parts.scheme == "http" and not _is_local_host(host):
        raise ProviderError("url_insecure", host)
    path = parts.path.rstrip("/")
    # People often paste a full endpoint instead of the base.
    for suffix in ("/chat/completions", "/responses", "/completions", "/models"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


def base_candidates(base: str) -> list[str]:
    path = urlsplit(base).path
    if re.search(r"/v\d+[a-z0-9.]*$", path, re.IGNORECASE) or path.lower().endswith("/openai"):
        return [base]
    return [base, base + "/v1"]


# ---------------------------------------------------------------- client

class ProviderClient:
    def __init__(self, base_url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url
        self.api_key = api_key
        self._client = httpx.Client(
            timeout=httpx.Timeout(TEST_TIMEOUT, connect=CONNECT_TIMEOUT),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    # -- stage 1
    def check_reachable(self) -> None:
        try:
            self._client.get(self.base_url, timeout=CONNECT_TIMEOUT)
        except httpx.ConnectError as e:
            msg = str(e)
            if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper():
                raise ProviderError("tls_error", msg[:200]) from e
            raise ProviderError("unreachable", msg[:200]) from e
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", str(e)[:200]) from e
        except httpx.HTTPError as e:
            raise ProviderError("unreachable", str(e)[:200]) from e

    # -- stages 2-3
    def discover_models(self, report: ProbeReport) -> None:
        last_status = None
        for cand in base_candidates(self.base_url):
            url = cand + "/models"
            try:
                r = self._client.get(url, headers=self._auth(), timeout=30.0)
            except httpx.HTTPError as e:
                raise ProviderError("unreachable", str(e)[:200]) from e
            last_status = r.status_code
            log.info("GET %s -> %s", url, r.status_code)
            if r.status_code in (401, 403):
                raise ProviderError("key_invalid", _error_text(r))
            if r.status_code in (301, 302, 307, 308):
                raise ProviderError("redirect", r.headers.get("location", ""))
            if r.status_code == 200:
                ids = _parse_models(r)
                if ids is None:
                    continue
                report.base_url = cand
                self.base_url = cand
                report.models_url = url
                report.key_verified = True
                ids = self._paginate(url, r, ids)
                report.discovered = ids
                report.context = _context_windows(r)
                if not ids:
                    raise ProviderError("no_models")
                return
        raise ProviderError("models_endpoint_missing", f"HTTP {last_status}")

    def _paginate(self, url: str, first: httpx.Response, ids: list[str]) -> list[str]:
        data = _safe_json(first) or {}
        pages = 0
        while isinstance(data, dict) and data.get("has_more") and data.get("last_id") and pages < 20:
            pages += 1
            r = self._client.get(url, headers=self._auth(), params={"after": data["last_id"]}, timeout=30.0)
            if r.status_code != 200:
                break
            more = _parse_models(r) or []
            new = [m for m in more if m not in ids]
            if not new:
                break
            ids.extend(new)
            data = _safe_json(r) or {}
        return ids

    # -- stage 4
    def probe_chat(self, model: str) -> bool:
        try:
            r = self._client.post(
                self.base_url + "/chat/completions",
                headers=self._auth(),
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                timeout=60.0,
            )
        except httpx.HTTPError:
            return False
        if r.status_code in (401, 403):
            raise ProviderError("key_invalid", _error_text(r))
        return r.status_code == 200

    def test_chat_tools(self, model: str) -> ModelResult:
        """Streamed Chat Completions call with a function tool (used for bridged providers)."""
        res = ModelResult(id=model, tested=True)
        body = {"model": model, "stream": True, "max_tokens": 32,
                "messages": [{"role": "system", "content": "You are a connectivity test."},
                             {"role": "user", "content": "Reply with OK."}],
                "tools": [{"type": "function", "function": {
                    "name": "noop", "description": "Does nothing.",
                    "parameters": {"type": "object", "properties": {}}}}],
                "tool_choice": "auto"}
        for attempt in range(2):
            try:
                with self._client.stream("POST", self.base_url + "/chat/completions",
                                         headers={**self._auth(), "Accept": "text/event-stream"}, json=body) as r:
                    if r.status_code in (401, 403):
                        r.read()
                        raise ProviderError("key_invalid", _error_text(r))
                    if r.status_code == 429 and attempt == 0:
                        r.read()
                        time.sleep(3)
                        continue
                    if r.status_code != 200:
                        r.read()
                        res.http_status = r.status_code
                        res.reason = f"HTTP {r.status_code}: {_error_text(r)}"
                        return res
                    ctype = r.headers.get("content-type", "")
                    if "event-stream" not in ctype:
                        r.read()
                        data = _safe_json(r)
                        ok = isinstance(data, dict) and bool(data.get("choices"))
                        res.compatible, res.reason = ok, ("ok" if ok else "unexpected response")
                        return res
                    got_choice = done = False
                    size = 0
                    for line in r.iter_lines():
                        size += len(line)
                        if size > 2_000_000:
                            break
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            done = True
                            break
                        try:
                            obj = json.loads(payload)
                        except ValueError:
                            continue
                        if isinstance(obj, dict) and obj.get("error"):
                            res.reason = "stream reported an error"
                            return res
                        for ch in (obj.get("choices") or []) if isinstance(obj, dict) else []:
                            got_choice = True
                            if ch.get("finish_reason"):
                                done = True
                    res.compatible = got_choice and done
                    res.reason = "ok" if res.compatible else "stream ended early"
                    return res
            except httpx.TimeoutException:
                res.reason = "timeout"
                return res
            except httpx.HTTPError as e:
                res.reason = str(e)[:160]
                return res
        res.reason = "rate limited (HTTP 429)"
        return res

    def test_responses(self, model: str) -> ModelResult:
        """Stream a real Responses request that carries a function tool, like Codex."""
        res = ModelResult(id=model, tested=True)
        body = {
            "model": model,
            "instructions": "You are a connectivity test. Reply with the single word OK.",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "Reply with OK."}]}],
            "tools": [{
                "type": "function", "name": "noop", "description": "Does nothing.",
                "strict": False,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
            "max_output_tokens": 64,
        }
        for attempt in range(2):
            try:
                with self._client.stream(
                    "POST", self.base_url + "/responses",
                    headers={**self._auth(), "Accept": "text/event-stream"},
                    json=body,
                ) as r:
                    if r.status_code in (401, 403):
                        r.read()
                        raise ProviderError("key_invalid", _error_text(r))
                    if r.status_code == 429 and attempt == 0:
                        r.read()
                        time.sleep(3)
                        continue
                    if r.status_code != 200:
                        r.read()
                        res.reason = f"HTTP {r.status_code}: {_error_text(r)}"
                        res.http_status = r.status_code
                        return res
                    ctype = r.headers.get("content-type", "")
                    if "event-stream" not in ctype:
                        r.read()
                        res.reason = f"no streaming (content-type {ctype or 'missing'})"
                        return res
                    events = _read_sse_types(r)
            except httpx.TimeoutException:
                res.reason = "timeout"
                return res
            except httpx.HTTPError as e:
                res.reason = str(e)[:160]
                return res
            if "response.failed" in events or "error" in events:
                res.reason = "stream reported an error"
            elif events & {"response.completed", "response.incomplete", "response.done"}:
                res.compatible = True
                res.reason = "ok"
            else:
                res.reason = "stream ended without response.completed"
            return res
        res.reason = "rate limited (HTTP 429)"
        return res


def is_model_specific(reason: str) -> bool:
    r = reason.lower()
    return "model" in r and ("not found" in r or "does not exist" in r or "unknown" in r or "not support" in r)


def _read_sse_types(r: httpx.Response, limit_bytes: int = 4_000_000) -> set[str]:
    seen: set[str] = set()
    size = 0
    for line in r.iter_lines():
        size += len(line)
        if size > limit_bytes:
            break
        if line.startswith("event:"):
            seen.add(line[6:].strip())
        elif line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict) and isinstance(obj.get("type"), str):
                        seen.add(obj["type"])
                except ValueError:
                    pass
        if seen & {"response.completed", "response.incomplete", "response.failed", "response.done"}:
            break
    return seen


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return None


def _parse_models(r: httpx.Response) -> list[str] | None:
    data = _safe_json(r)
    items = None
    if isinstance(data, dict):
        items = data.get("data")
        if items is None:
            items = data.get("models")
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return None
    ids: list[str] = []
    for it in items:
        mid = None
        if isinstance(it, str):
            mid = it
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("slug") or it.get("name")
            if _metadata_says_incompatible(it):
                mid = None if mid is None else f"\x00{mid}"
        if isinstance(mid, str) and mid.strip() and mid not in ids:
            ids.append(mid.strip())
    return ids


def _context_windows(r: httpx.Response) -> dict:
    data = _safe_json(r)
    items = data.get("data") or data.get("models") if isinstance(data, dict) else data
    out = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        mid = it.get("id") or it.get("slug") or it.get("name")
        for k in ("context_length", "context_window", "max_context_length", "max_model_len", "max_input_tokens"):
            v = it.get(k) or (it.get("top_provider") or {}).get(k)
            if isinstance(v, (int, float)) and v > 0:
                out[str(mid)] = int(v)
                break
    return out


def _metadata_says_incompatible(it: dict) -> bool:
    """Respect explicit provider metadata (e.g. OpenRouter supported_parameters)."""
    sp = it.get("supported_parameters")
    if isinstance(sp, list) and sp and "tools" not in sp:
        return True
    arch = it.get("architecture")
    if isinstance(arch, dict):
        out = arch.get("output_modalities")
        if isinstance(out, list) and out and "text" not in out:
            return True
    return False


def _error_text(r: httpx.Response) -> str:
    data = _safe_json(r)
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:240]
        if isinstance(err, str):
            return err[:240]
        if data.get("message"):
            return str(data["message"])[:240]
    return (r.text or "").strip()[:240]


def _priority(mid: str) -> tuple[int, str]:
    m = mid.lower()
    for i, key in enumerate(("codex", "gpt-5", "gpt-4.1", "o4", "o3", "claude", "gemini", "qwen", "deepseek", "glm", "kimi")):
        if key in m:
            return (i, m)
    return (99, m)


def select_candidates(discovered: list[str]) -> tuple[list[str], list[str]]:
    """Split discovered ids into agent candidates and obvious non-agent models."""
    cands, rejected = [], []
    for mid in discovered:
        if mid.startswith("\x00"):
            rejected.append(mid[1:])
        elif _NON_AGENT.search(mid):
            rejected.append(mid)
        else:
            cands.append(mid)
    cands.sort(key=_priority)
    return cands, rejected


def pick_default(compatible: list[str]) -> str:
    return sorted(compatible, key=_priority)[0]


def run_compat_tests(client: ProviderClient, models: list[str], on_progress=None,
                     bridged: bool = False) -> list[ModelResult]:
    results: list[ModelResult] = []
    done = 0

    def one(mid: str) -> ModelResult:
        return client.test_chat_tools(mid) if bridged else client.test_responses(mid)

    with ThreadPoolExecutor(max_workers=TEST_CONCURRENCY) as ex:
        futures = [ex.submit(one, m) for m in models]
        for f in futures:
            res = f.result()  # re-raises ProviderError(key_invalid)
            results.append(res)
            done += 1
            if on_progress:
                on_progress(done, len(models), res)
    return results
