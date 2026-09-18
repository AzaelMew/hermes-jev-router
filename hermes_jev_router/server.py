"""Local OpenAI-compatible sidecar for Jev-routed Hermes requests.

The sidecar is deliberately dependency-free. It is small enough to run beside
Hermes on a laptop and portable enough to become the OpenClaw transport later.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import html
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import RouteTarget, RouterConfig, TIERS, load_config
from .decision import (
    Decision,
    classify_request,
    decode_marker,
    encode_marker,
    format_route_card,
    strip_markers,
    strip_route_cards,
)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
        )
    return ""


def _clean_content(content: Any) -> Any:
    if isinstance(content, str):
        return strip_route_cards(strip_markers(content))
    if isinstance(content, list):
        cleaned = []
        for part in content:
            if not isinstance(part, dict):
                cleaned.append(part)
                continue
            item = dict(part)
            if item.get("type") in {"text", "input_text"} and isinstance(item.get("text"), str):
                item["text"] = strip_route_cards(strip_markers(item["text"]))
            cleaned.append(item)
        return cleaned
    return content


def clean_messages(messages: list[Any]) -> list[Any]:
    """Remove the internal hook marker before a request reaches the LLM."""

    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        item = dict(message)
        if "content" in item:
            item["content"] = _clean_content(item["content"])
        cleaned.append(item)
    return cleaned


def _last_user_message(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _text_from_content(message.get("content", ""))
    return ""


def _decision_from_messages(messages: list[Any]) -> Decision | None:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        decision = decode_marker(_text_from_content(message.get("content", "")))
        if decision:
            return decision
    return None


def _manual_decision(tier: str) -> Decision:
    return Decision(
        tier=tier,
        confidence=1.0,
        probabilities={tier: 1.0},
        source="manual",
        reasons=[f"Manual {tier} route requested"],
    )


def _requested_tier(model: str) -> str | None:
    value = (model or "").strip().lower()
    if value.startswith("jev-router/"):
        value = value.split("/", 1)[1]
    if value in TIERS:
        return value
    return None


def _find_target(config: RouterConfig, model: str) -> RouteTarget | None:
    for target in config.routes.values():
        if target.model == model:
            return target
    return None


def _next_tool_capable(config: RouterConfig, tier: str) -> RouteTarget | None:
    start = TIERS.index(tier)
    for candidate in TIERS[start:]:
        target = config.routes.get(candidate)
        if target and target.supports_tools:
            return target
    return None


def _route_target(config: RouterConfig, decision: Decision, tools_present: bool) -> tuple[Decision, RouteTarget | None, str | None]:
    target = None
    if isinstance(decision.target, dict):
        try:
            target = RouteTarget.from_dict(decision.tier, decision.target)
        except ValueError:
            # A marker may come from an older plugin. Ignore an invalid target
            # and use the local sidecar route table instead.
            target = None
    if target is None:
        target = config.routes.get(decision.tier)
    if tools_present and target and not target.supports_tools:
        capable = _next_tool_capable(config, decision.tier)
        if capable:
            decision = Decision.from_payload(decision.to_payload())
            decision.tier = capable.tier
            decision.promoted = True
            decision.reasons.insert(0, f"Tool calls require the {capable.tier} route")
            target = capable
    if target is None:
        return decision, None, f"No downstream target is configured for {decision.tier}"
    if target.base_url.startswith("demo://"):
        return decision, target, None
    if not target.api_key_env:
        return decision, target, None
    if not os.getenv(target.api_key_env):
        return decision, target, f"{target.api_key_env} is not set for the {decision.tier} route"
    return decision, target, None


def _model_entry(model_id: str, description: str) -> dict[str, Any]:
    return {"id": model_id, "object": "model", "owned_by": "hermes-jev-router", "description": description}


def _demo_response(body: dict[str, Any], card: str, target: RouteTarget) -> dict[str, Any]:
    content = (
        f"{card}\n\n"
        "[Demo upstream]\n"
        f"This local rehearsal selected {target.model}. Configure a real base_url and api_key_env for live answers."
    )
    return {
        "id": f"jev-router-demo-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": target.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "jev_router": {"demo": True, "requested_model": body.get("model", "auto")},
    }


def _stream_chunk(model: str, content: str, *, role: str | None = None, finish_reason: str | None = None) -> bytes:
    delta: dict[str, Any] = {"content": content}
    if role:
        delta["role"] = role
    payload = {
        "id": f"jev-router-{int(time.time() * 1000)}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump())
    if hasattr(value, "__dict__"):
        return {str(key): _jsonable(item) for key, item in vars(value).items() if not str(key).startswith("_")}
    return str(value)


def _append_decision_marker(messages: list[Any], decision: Decision) -> list[Any]:
    marked = copy.deepcopy(messages)
    marker = encode_marker(decision)
    for message in reversed(marked):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            message["content"] = content + "\n" + marker
        elif isinstance(content, list):
            content.append({"type": "text", "text": marker})
        else:
            message["content"] = marker
        break
    return marked


class RouterHandler(BaseHTTPRequestHandler):
    """HTTP handler. The config is stored on the server instance."""

    server_version = "HermesJevRouter/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def config(self) -> RouterConfig:
        return self.server.router_config  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[jev-router] {format % args}")

    def _authorized(self) -> bool:
        expected = self.config.router_api_key
        if not expected:
            return True
        supplied = self.headers.get("Authorization", "")
        return supplied == f"Bearer {expected}"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 20_000_000:
                return None
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._authorized():
            self._send_json(401, {"error": {"message": "Invalid local router key", "type": "authentication_error"}})
            return
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "hermes-jev-router",
                    "jev_configured": bool(self.config.jev_api_key),
                    "demo_mode": self.config.demo_mode,
                    "routes": {
                        tier: {
                            "model": target.model,
                            "base_url": target.base_url,
                            "api_key_env": target.api_key_env,
                            "api_key_present": bool(target.api_key_env and os.getenv(target.api_key_env)),
                            "supports_tools": target.supports_tools,
                        }
                        for tier, target in self.config.routes.items()
                    },
                },
            )
            return
        if self.path in {"/v1/models", "/models"}:
            data = [
                _model_entry("auto", "Let Jev choose the least expensive capable Hermes route"),
                _model_entry("micro", "Force the fastest, lowest-cost route"),
                _model_entry("cheap", "Force the cheap route"),
                _model_entry("balanced", "Force the balanced route"),
                _model_entry("strong", "Force the strong reasoning route"),
                _model_entry("frontier", "Force the frontier route"),
            ]
            data.extend(_model_entry(target.model, f"Configured {tier} target") for tier, target in self.config.routes.items())
            self._send_json(200, {"object": "list", "data": data})
            return
        if self.path == "/":
            self._send_html()
            return
        self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})

    def _send_html(self) -> None:
        rows = "".join(
            f"<tr><td>{html.escape(tier)}</td><td>{html.escape(target.model)}</td>"
            f"<td>{html.escape(target.base_url)}</td><td>{'yes' if target.api_key_env and os.getenv(target.api_key_env) else 'no'}</td></tr>"
            for tier, target in self.config.routes.items()
        )
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Jev Router</title><style>
:root {{ color-scheme: dark; --bg:#0b1020; --panel:#151d35; --ink:#f5f7ff; --muted:#9ba8c7; --accent:#7cf3d0; }}
body {{ margin:0; min-height:100vh; font:15px/1.5 ui-sans-serif,system-ui,sans-serif; color:var(--ink); background:radial-gradient(circle at 20% 0%,#1b3152,var(--bg) 45%); }}
main {{ max-width:920px; margin:0 auto; padding:56px 24px; }}
.eyebrow {{ color:var(--accent); letter-spacing:.16em; text-transform:uppercase; font-size:12px; font-weight:700; }}
h1 {{ font-size:clamp(2.3rem,6vw,4.5rem); line-height:1; margin:12px 0 18px; }}
p {{ color:var(--muted); max-width:680px; }}
.card {{ background:color-mix(in srgb,var(--panel) 88%,transparent); border:1px solid #2b3a60; border-radius:20px; padding:24px; margin-top:28px; box-shadow:0 20px 80px #02061266; }}
code {{ color:var(--accent); }} table {{ width:100%; border-collapse:collapse; margin-top:10px; }} th,td {{ text-align:left; padding:12px 8px; border-bottom:1px solid #2b3a60; }} th {{ color:var(--muted); font-size:12px; text-transform:uppercase; }}
.flow {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:18px; }} .step {{ padding:10px 14px; border-radius:999px; background:#203052; }} .arrow {{ color:var(--accent); }}
</style></head><body><main><div class="eyebrow">Hermes · TypeSafe AI</div>
<h1>Jev chooses the route.</h1><p>This local sidecar evaluates each request once, shows the decision first, and forwards the clean request to the selected model.</p>
<div class="flow"><span class="step">Your message</span><span class="arrow">→</span><span class="step">Jev decision</span><span class="arrow">→</span><span class="step">Cheap / balanced / frontier</span></div>
<section class="card"><h2>Configured targets</h2><table><thead><tr><th>Tier</th><th>Model</th><th>Endpoint</th><th>Key present</th></tr></thead><tbody>{rows}</tbody></table></section>
<section class="card"><h2>Local endpoints</h2><p><code>GET /health</code> · <code>GET /v1/models</code> · <code>POST /v1/chat/completions</code></p></section>
</main></body></html>"""
        body_bytes = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._authorized():
            self._send_json(401, {"error": {"message": "Invalid local router key", "type": "authentication_error"}})
            return
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "Not found", "type": "invalid_request_error"}})
            return
        body = self._read_json()
        if not body or not isinstance(body.get("messages"), list):
            self._send_json(400, {"error": {"message": "messages must be a JSON array", "type": "invalid_request_error"}})
            return
        messages = body["messages"]
        user_message = _last_user_message(messages)
        if not user_message:
            self._send_json(400, {"error": {"message": "At least one user message is required", "type": "invalid_request_error"}})
            return
        requested_model = str(body.get("model", "auto"))
        manual_tier = _requested_tier(requested_model)
        if manual_tier:
            decision = _manual_decision(manual_tier)
        else:
            target_by_model = _find_target(self.config, requested_model)
            if target_by_model:
                target = target_by_model
                decision = _manual_decision(target.tier)
            else:
                decision = _decision_from_messages(messages)
                if not decision:
                    decision = classify_request(
                        user_message,
                        messages,
                        platform="hermes",
                        model=requested_model,
                        tools_present=bool(body.get("tools")),
                        config=self.config,
                    )
        decision, target, error = _route_target(self.config, decision, bool(body.get("tools")))
        if error and not self.config.demo_mode:
            self._send_json(503, {"error": {"message": error, "type": "router_configuration_error"}})
            return
        if target is None:
            self._send_json(503, {"error": {"message": error or "No route target", "type": "router_configuration_error"}})
            return
        clean_body = dict(body)
        clean_body["messages"] = clean_messages(messages)
        clean_body["model"] = target.model
        if clean_body.get("stream") and not target.supports_streaming:
            clean_body["stream"] = False
            decision.reasons.insert(0, "Target does not support streaming; used a complete response")
        card = format_route_card(decision, target.model, platform="telegram" if "telegram" in requested_model else "cli")
        if self.config.demo_mode or target.base_url.startswith("demo://"):
            if body.get("stream"):
                self._serve_demo_stream(target, card)
            else:
                self._send_json(200, _demo_response(body, card, target))
            return
        if self._forward_native(body, decision, target):
            return
        self._forward(clean_body, target, card)

    def _serve_demo_stream(self, target: RouteTarget, card: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(_stream_chunk(target.model, card + "\n\n", role="assistant"))
        self.wfile.write(_stream_chunk(target.model, "[Demo upstream]\nThis is a local routing rehearsal."))
        self.wfile.write(_stream_chunk(target.model, "", finish_reason="stop"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _forward(self, body: dict[str, Any], target: RouteTarget, card: str) -> None:
        api_key = os.getenv(target.api_key_env) if target.api_key_env else None
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if body.get("stream") else "application/json",
            "User-Agent": "hermes-jev-router/0.1",
            **target.headers,
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(
            target.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            response = urlopen(request, timeout=self.config.upstream_timeout)
        except HTTPError as error:
            detail = " ".join(error.read(512).decode("utf-8", errors="replace").split())[:240]
            self._send_json(error.code, {"error": {"message": detail or "Upstream request failed", "type": "upstream_error"}})
            return
        except (URLError, TimeoutError, OSError) as error:
            self._send_json(502, {"error": {"message": str(error)[:240], "type": "upstream_error"}})
            return
        with response:
            if body.get("stream"):
                self._forward_stream(response, target, card)
            else:
                self._forward_json(response, target, card)

    def _forward_native(self, body: dict[str, Any], decision: Decision, target: RouteTarget) -> bool:
        """Use Hermes' own credential resolver and native transport when available."""

        if not isinstance(decision.target, dict):
            return False
        try:
            from router_client import JevRouterClient
        except ImportError:
            return False
        try:
            native_body = dict(body)
            native_body["messages"] = _append_decision_marker(body["messages"], decision)
            client = JevRouterClient({"timeout": self.config.upstream_timeout})
            response = client.chat.completions.create(**native_body)
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in response:
                    payload = _jsonable(chunk)
                    self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                payload = _jsonable(response)
                if not isinstance(payload, dict):
                    payload = {"data": payload}
                self._send_json(200, payload)
            return True
        except Exception as error:
            self._send_json(502, {"error": {"message": f"Hermes native route failed: {str(error)[:220]}", "type": "upstream_error"}})
            return True

    def _forward_stream(self, response: Any, target: RouteTarget, card: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(_stream_chunk(target.model, card + "\n\n", role="assistant"))
        self.wfile.flush()
        while True:
            line = response.readline()
            if not line:
                break
            self.wfile.write(line)
            self.wfile.flush()

    def _forward_json(self, response: Any, target: RouteTarget, card: str) -> None:
        try:
            payload = json.loads(response.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(502, {"error": {"message": "Upstream returned invalid JSON", "type": "upstream_error"}})
            return
        if isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    message["content"] = card + "\n\n" + (content or "")
            payload.setdefault("jev_router", {})
            if isinstance(payload["jev_router"], dict):
                payload["jev_router"].update({"tier": target.tier, "model": target.model})
        self._send_json(200, payload if isinstance(payload, dict) else {"data": payload})


class RouterServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: RouterConfig):
        super().__init__(address, RouterHandler)
        self.router_config = config


def build_server(config: RouterConfig | None = None) -> RouterServer:
    host = os.getenv("JEV_ROUTER_HOST", "127.0.0.1")
    port = int(os.getenv("JEV_ROUTER_PORT", "8765"))
    return RouterServer((host, port), config or load_config())


def main() -> None:
    server = build_server()
    host, port = server.server_address
    print(f"Jev router listening on http://{host}:{port}")
    print("Decision card is emitted before every downstream response.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Jev router.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


# Created by Codex GPT-6 on 2026-09-17 11:03 PDT on ombee.
