"""Small OpenAI-compatible JSON client with auditable usage accounting."""
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .telemetry import ExecutionLedger


def _strip_code_fence(value: str) -> str:
    text = value.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _repair_truncated_json(text: str) -> Optional[str]:
    """Close an output that was cut off mid-object so a partial answer survives."""
    start = text.find("{")
    if start < 0:
        return None
    out: List[str] = []
    stack: List[str] = []
    in_string = False
    escaped = False
    for char in text[start:]:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                return None
            stack.pop()
        out.append(char)
    if in_string:
        out.append('"')
    for opener in reversed(stack):
        out.append("}" if opener == "{" else "]")
    return "".join(out)


def decode_json_object(content: str) -> Dict[str, Any]:
    """Extract one JSON object from model output.

    Providers occasionally wrap JSON in code fences, append prose, emit two
    objects back-to-back, or truncate the answer at the token limit.  A strict
    ``json.loads`` turns all of those into a failed call and, for agent roles,
    a failed review step.  Accept the first decodable object instead.
    """
    text = _strip_code_fence(str(content))
    decoder = json.JSONDecoder()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start >= 0:
        # Prefer the outermost object: decoding a nested object would return a
        # fragment such as one finding instead of the full action payload.
        try:
            value, _end = decoder.raw_decode(text, start)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        repaired = _repair_truncated_json(text)
        if repaired is not None:
            try:
                value = json.loads(repaired)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict):
                return value
    index = text.find("{", start + 1) if start >= 0 else -1
    while index != -1:
        try:
            value, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            return value
        index = text.find("{", index + 1)
    raise ValueError("model content is not a JSON object")


class _InvalidJson(ValueError):
    """The model answered, but the answer could not be decoded as JSON."""


class JsonChatClient:
    def __init__(
        self, base_url: str, api_key: str, model: str,
        provider: str = "openai-compatible", timeout: int = 60,
        extra_headers: Optional[Dict[str, str]] = None,
        retries: int = 2, retry_delay_seconds: float = 1.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.retries = max(0, int(retries))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(self.extra_headers)
        return headers

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(), method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _repair_json(
        self, payload: Dict[str, Any], content: str, role: str,
        ledger: Optional[ExecutionLedger],
    ) -> Optional[Dict[str, Any]]:
        """Ask once for the same answer reformatted as JSON.

        Repeating an identical request at temperature 0 rarely changes the
        output; a targeted repair turn usually does, and it costs one call
        instead of three failed retries plus a failed worker step.
        """
        messages = list(payload.get("messages") or [])
        messages.append({"role": "assistant", "content": str(content)[:6000]})
        messages.append({
            "role": "user",
            "content": (
                "The previous message was not valid JSON. Return the same answer as exactly "
                "one valid JSON object. Output JSON only: no prose and no markdown fences."
            ),
        })
        repair: Dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if payload.get("max_tokens"):
            repair["max_tokens"] = payload["max_tokens"]
        started = time.monotonic()
        try:
            body = self._post(repair)
            result = decode_json_object(body["choices"][0]["message"]["content"])
        except Exception:
            if ledger:
                ledger.record_model(
                    role, self.provider, self.model, {},
                    int((time.monotonic() - started) * 1000), False,
                    "JSON repair attempt failed",
                )
            return None
        if ledger:
            ledger.record_model(
                role, self.provider, self.model, body.get("usage") or {},
                int((time.monotonic() - started) * 1000), True,
            )
        return result

    def complete_json(
        self, role: str, system: str, user: str,
        ledger: Optional[ExecutionLedger] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        for attempt in range(1, self.retries + 2):
            started = time.monotonic()
            retryable = False
            try:
                body = self._post(payload)
                content = body["choices"][0]["message"]["content"]
                try:
                    result = decode_json_object(content)
                except ValueError as exc:
                    repaired = self._repair_json(payload, content, role, ledger)
                    if repaired is not None:
                        return repaired
                    raise _InvalidJson(str(exc)) from exc
                if ledger:
                    ledger.record_model(
                        role, self.provider, self.model, body.get("usage") or {},
                        int((time.monotonic() - started) * 1000), True,
                    )
                return result
            except _InvalidJson as exc:
                message = "%s JSON request failed: %s" % (self.provider, exc)
                if ledger:
                    ledger.record_model(
                        role, self.provider, self.model, {},
                        int((time.monotonic() - started) * 1000), False, message,
                    )
                # The repair turn already ran; repeating the identical request
                # is wasteful and should not trigger a whole-node retry.
                raise ValueError(message) from exc
            except urllib.error.HTTPError as exc:
                detail = exc.read(1000).decode("utf-8", errors="replace")
                message = "%s API returned HTTP %d: %s" % (
                    self.provider, exc.code, detail,
                )
                retryable = exc.code in {408, 429, 500, 502, 503, 504}
            except (urllib.error.URLError, socket.timeout, ValueError, KeyError,
                    IndexError, TypeError, json.JSONDecodeError) as exc:
                message = "%s JSON request failed: %s" % (self.provider, exc)
                retryable = True
            if ledger:
                ledger.record_model(
                    role, self.provider, self.model, {},
                    int((time.monotonic() - started) * 1000), False, message,
                )
            if attempt <= self.retries and retryable:
                time.sleep(self.retry_delay_seconds * attempt)
                continue
            raise RuntimeError(message)
