"""Native Hermes client that dispatches a Jev decision to a real Hermes model.

The provider profile is the execution seam. Hermes still owns credentials and
provider auth. This module only reads the selected provider/model from the
internal marker and asks Hermes to resolve that target.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import json
import re
from types import SimpleNamespace
from typing import Any, Iterable, Iterator


MARKER_RE = re.compile(r"\[JEV_ROUTER_DECISION_V1\](.*?)\[/JEV_ROUTER_DECISION_V1\]", re.DOTALL)
CARD_START = "╭─ ⚡ Jev model routing ─╮"
CARD_END = "╰────────────────────────╯"
ROUTER_PROVIDERS = {"jev-router", "jev", "auto-router"}
ALLOWED_MODES = {"chat_completions", "codex_responses", "anthropic_messages"}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
        )
    return ""


def _strip_markers(content: Any) -> Any:
    if isinstance(content, str):
        return MARKER_RE.sub("", content)
    if isinstance(content, list):
        cleaned = copy.deepcopy(content)
        for part in cleaned:
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}:
                if isinstance(part.get("text"), str):
                    part["text"] = MARKER_RE.sub("", part["text"])
        return cleaned
    return content


def _clean_messages(messages: list[Any]) -> list[Any]:
    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        item = dict(message)
        if "content" in item:
            item["content"] = _strip_markers(item["content"])
        cleaned.append(item)
    return cleaned


def _decision_payload(messages: list[Any]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        match = MARKER_RE.search(_content_text(message.get("content", "")))
        if not match:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _target(payload: dict[str, Any] | None) -> dict[str, Any]:
    target = payload.get("target") if isinstance(payload, dict) else None
    if not isinstance(target, dict):
        raise RuntimeError("Jev router did not return a Hermes model target")
    provider = str(target.get("provider") or "").strip().lower()
    model = str(target.get("model") or "").strip()
    base_url = str(target.get("base_url") or "").strip().rstrip("/")
    api_mode = str(target.get("api_mode") or "chat_completions").strip().lower()
    if not provider or provider in ROUTER_PROVIDERS:
        raise RuntimeError("Jev router returned an invalid downstream provider")
    if not model or not base_url or not base_url.startswith(("http://", "https://")):
        raise RuntimeError("Jev router returned an invalid downstream model target")
    if api_mode not in ALLOWED_MODES:
        raise RuntimeError(f"Jev router returned unsupported API mode: {api_mode}")
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_mode": api_mode,
        "reasoning_effort": target.get("reasoning_effort"),
        "fallback": target.get("fallback") if isinstance(target.get("fallback"), dict) else {},
        "supports_tools": bool(target.get("supports_tools", True)),
        "supports_streaming": bool(target.get("supports_streaming", True)),
    }


def _route_card(payload: dict[str, Any], target: dict[str, Any]) -> str:
    tier = str(payload.get("tier") or "balanced").upper()
    effort = target.get("reasoning_effort") or {
        "micro": "minimal", "cheap": "low", "balanced": "medium",
        "strong": "high", "frontier": "xhigh",
    }.get(str(payload.get("tier") or "balanced").lower())
    model = str(target.get("model") or "")
    provider = str(target.get("provider") or "")
    display_model = model.removeprefix("gpt-") if provider == "openai-codex" else model
    label = f"⚡ Jev · {tier} · {display_model}"
    if effort:
        label += f" · {effort}"
    reasons = payload.get("reasons")
    if isinstance(reasons, list) and reasons and "fallback" in str(reasons[0]).lower():
        label += " · fallback"
    return label


def _openai_client(runtime: dict[str, Any], client_kwargs: dict[str, Any]) -> Any:
    from agent.auxiliary_client import _create_openai_client

    kwargs = {
        "api_key": str(runtime.get("api_key") or "hermes-local"),
        "base_url": str(runtime["base_url"]),
        "max_retries": 0,
    }
    for key in ("timeout", "default_headers", "http_client"):
        if client_kwargs.get(key) is not None:
            kwargs[key] = client_kwargs[key]
    return _create_openai_client(**kwargs)


def _downstream_client(target: dict[str, Any], runtime: dict[str, Any], client_kwargs: dict[str, Any]) -> Any:
    mode = str(runtime.get("api_mode") or target["api_mode"])
    if mode == "chat_completions":
        if target["provider"] == "gemini":
            from agent.gemini_native_adapter import GeminiNativeClient

            return GeminiNativeClient(
                api_key=str(runtime.get("api_key") or ""),
                base_url=str(runtime["base_url"]),
                default_headers=client_kwargs.get("default_headers"),
                timeout=client_kwargs.get("timeout"),
            )
        return _openai_client(runtime, client_kwargs)
    if mode == "codex_responses":
        from agent.auxiliary_client import CodexAuxiliaryClient

        return CodexAuxiliaryClient(
            _openai_client(runtime, client_kwargs), target["model"]
        )
    if mode == "anthropic_messages":
        from anthropic import Anthropic
        from agent.auxiliary_client import AnthropicAuxiliaryClient

        real_client = Anthropic(
            api_key=str(runtime.get("api_key") or ""),
            base_url=str(runtime["base_url"]),
            timeout=client_kwargs.get("timeout"),
            default_headers=client_kwargs.get("default_headers"),
            max_retries=0,
        )
        return AnthropicAuxiliaryClient(
            real_client, target["model"], str(runtime.get("api_key") or ""),
            str(runtime["base_url"]), is_oauth=target["provider"] in {"anthropic-oauth", "claude-oauth"},
        )
    raise RuntimeError(f"Unsupported Hermes target API mode: {mode}")


def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _should_failover(error: Exception) -> bool:
    text = str(error).lower()
    return any(token in text for token in (
        "401", "402", "403", "404", "429", "authentication_error", "api key is invalid",
        "model_not_available", "model_not_supported", "model is not supported",
        "model not found", "unknown model", "insufficient credit", "quota",
        "rate limit", "rate_limit", "ratelimit", "too many requests", "usage limit",
        "guardrail", "data policy",
    ))


def _with_card(response: Any, card: str) -> Any:
    choices = getattr(response, "choices", None)
    if not choices:
        return response
    message = getattr(choices[0], "message", None)
    if message is None:
        return response
    content = getattr(message, "content", None)
    if not content:
        return response
    message.content = f"{card}\n\n{content}"
    return response


def _chunk(
    model: str,
    content: str,
    *,
    role: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list[Any] | None = None,
) -> Any:
    delta: dict[str, Any] = {"content": content}
    if role:
        delta["role"] = role
    if tool_calls:
        delta["tool_calls"] = tool_calls
    return SimpleNamespace(
        id="jev-router-stream",
        object="chat.completion.chunk",
        model=model,
        choices=[SimpleNamespace(index=0, delta=SimpleNamespace(**delta), finish_reason=finish_reason)],
    )


def _tool_call_deltas(message: Any) -> list[Any]:
    result = []
    for index, raw in enumerate(getattr(message, "tool_calls", None) or []):
        if isinstance(raw, dict):
            function = raw.get("function") or {}
            call_id = raw.get("id")
            name = function.get("name")
            arguments = function.get("arguments", "")
        else:
            function = getattr(raw, "function", None)
            call_id = getattr(raw, "id", None) or getattr(raw, "call_id", None)
            name = getattr(function, "name", None) or getattr(raw, "name", None)
            arguments = getattr(function, "arguments", None) or getattr(raw, "arguments", "")
        result.append(SimpleNamespace(
            index=index,
            id=call_id,
            type="function",
            function=SimpleNamespace(name=name or "", arguments=arguments or ""),
        ))
    return result


@dataclass
class JevRouterClient:
    """OpenAI-compatible client facade used by Hermes' provider profile."""

    client_kwargs: dict[str, Any]

    def __post_init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.api_key = str(self.client_kwargs.get("api_key") or "")
        self.base_url = str(self.client_kwargs.get("base_url") or "")

    def _create(self, **kwargs: Any) -> Any:
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            raise RuntimeError("Jev router requires a messages list")
        payload = _decision_payload(messages)
        target = _target(payload)
        candidates = [(target, payload or {})]
        fallback = target.get("fallback") or {}
        if fallback.get("provider") and fallback.get("model") and fallback.get("base_url"):
            fallback_target = _target({
                "target": {
                    **fallback,
                    "reasoning_effort": fallback.get("reasoning_effort") or target.get("reasoning_effort"),
                    "api_mode": fallback.get("api_mode") or target["api_mode"],
                    "supports_tools": target["supports_tools"],
                    "supports_streaming": target["supports_streaming"],
                }
            })
            fallback_payload = dict(payload or {})
            fallback_payload["tier"] = payload.get("tier", "balanced") if payload else "balanced"
            fallback_payload["reasons"] = [
                f"Primary unavailable; using configured fallback {fallback_target['provider']}/{fallback_target['model']}"
            ]
            fallback_payload["promoted"] = True
            candidates.append((fallback_target, fallback_payload))
        for raw in (payload or {}).get("alternates", []):
            if not isinstance(raw, dict):
                continue
            alternate = _target({"target": raw})
            if alternate["provider"] == target["provider"] and alternate["model"] == target["model"]:
                continue
            alternate_payload = dict(payload or {})
            alternate_payload["tier"] = raw.get("tier") or alternate_payload.get("tier") or "balanced"
            alternate_payload["reasons"] = [
                f"Selected target was unavailable; used {alternate['provider']}/{alternate['model']} fallback"
            ] + list(alternate_payload.get("reasons") or [])
            alternate_payload["promoted"] = True
            candidates.append((alternate, alternate_payload))
        last_error: Exception | None = None
        for index, (candidate, candidate_payload) in enumerate(candidates):
            try:
                return self._dispatch(candidate, candidate_payload, kwargs, messages)
            except Exception as error:
                last_error = error
                if index + 1 >= len(candidates) or not _should_failover(error):
                    raise
        raise last_error or RuntimeError("Jev router could not dispatch the request")

    def _dispatch(
        self,
        target: dict[str, Any],
        payload: dict[str, Any],
        kwargs: dict[str, Any],
        messages: list[Any],
    ) -> Any:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(
            requested=target["provider"],
            explicit_base_url=target["base_url"],
            target_model=target["model"],
        )
        resolved_provider = str(runtime.get("provider") or "").strip().lower()
        if resolved_provider in ROUTER_PROVIDERS:
            raise RuntimeError("Hermes resolved the Jev route back to a router provider")
        target["api_mode"] = str(runtime.get("api_mode") or target["api_mode"])
        downstream = _downstream_client(target, runtime, self.client_kwargs)
        request = dict(kwargs)
        request["messages"] = _clean_messages(messages)
        request["model"] = target["model"]
        effort = target.get("reasoning_effort") or {
            "micro": "minimal", "cheap": "low", "balanced": "medium",
            "strong": "high", "frontier": "xhigh",
        }.get(str(payload.get("tier") or "balanced").lower())
        extra_body = request.get("extra_body")
        existing_reasoning = extra_body.get("reasoning") if isinstance(extra_body, dict) else None
        if effort:
            if target["api_mode"] == "codex_responses":
                request["extra_body"] = {
                    **(extra_body if isinstance(extra_body, dict) else {}),
                    "reasoning": {**(existing_reasoning if isinstance(existing_reasoning, dict) else {}), "effort": effort, "enabled": effort != "none"},
                }
            elif target["api_mode"] == "anthropic_messages":
                request["_reasoning_config"] = {"effort": effort, "enabled": effort != "none"}
            else:
                request["reasoning_effort"] = effort
        card = _route_card(payload, target)
        stream = bool(request.get("stream"))
        if stream and target["api_mode"] == "chat_completions" and target["supports_streaming"]:
            upstream = downstream.chat.completions.create(**request)
            return _stream_with_card(upstream, card, downstream)
        try:
            if stream:
                request["stream"] = False
                response = downstream.chat.completions.create(**request)
                choice = response.choices[0] if getattr(response, "choices", None) else None
                message = getattr(choice, "message", None)
                content = getattr(message, "content", None) if message is not None else ""
                tool_calls = _tool_call_deltas(message) if message is not None else []
                output = [
                    _chunk(target["model"], card + "\n\n", role="assistant"),
                    _chunk(target["model"], content or "", tool_calls=tool_calls),
                    _chunk(target["model"], "", finish_reason=getattr(choice, "finish_reason", "stop")),
                ]
                return _close_after(output, downstream)
            response = downstream.chat.completions.create(**request)
            return _with_card(response, card)
        finally:
            _close(downstream)

    def close(self) -> None:
        pass


def _close_after(chunks: Iterable[Any], downstream: Any) -> Iterator[Any]:
    try:
        yield from chunks
    finally:
        _close(downstream)


def _stream_chunk_has_text(chunk: Any) -> bool:
    choices = chunk.get("choices") if isinstance(chunk, dict) else getattr(chunk, "choices", None)
    if not choices:
        return False
    choice = choices[0]
    delta = choice.get("delta") if isinstance(choice, dict) else getattr(choice, "delta", None)
    content = delta.get("content") if isinstance(delta, dict) else getattr(delta, "content", None)
    return bool(content)


def _stream_with_card(upstream: Iterable[Any], card: str, downstream: Any) -> Iterator[Any]:
    try:
        leading = []
        for chunk in upstream:
            if _stream_chunk_has_text(chunk):
                yield _chunk("jev-router", card + "\n\n", role="assistant")
                yield from leading
                yield chunk
                break
            leading.append(chunk)
        else:
            yield from leading
            return
        yield from upstream
    finally:
        close = getattr(upstream, "close", None)
        if callable(close):
            close()
        _close(downstream)


# Created by Codex GPT-6 on 2026-09-17 16:09 PDT on ombee.
