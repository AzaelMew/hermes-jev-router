"""Typed Jev evaluation and explainable route decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import RouterConfig, TIERS


MARKER_START = "[JEV_ROUTER_DECISION_V1]"
MARKER_END = "[/JEV_ROUTER_DECISION_V1]"
MARKER_RE = re.compile(re.escape(MARKER_START) + r"(.*?)" + re.escape(MARKER_END), re.DOTALL)
CARD_START = "╭─ ⚡ Jev model routing ─╮"
CARD_END = "╰────────────────────────╯"
CARD_RE = re.compile(re.escape(CARD_START) + r".*?" + re.escape(CARD_END), re.DOTALL)


ROUTER_QUESTIONS: dict[str, dict[str, Any]] = {
    "route_tier": {
        "type": "choice",
        "instructions": (
            "Which available Hermes model tier best fits the user's current request? "
            "Choose the minimum tier that can answer well. The available model list "
            "is authoritative."
        ),
        "criteria": {
            "micro": (
                "The shortest possible answer, a greeting, a simple lookup, or a tiny "
                "rewrite. Use the lowest-cost capable model."
            ),
            "cheap": (
                "A normal short answer, simple explanation, light transformation, or "
                "small code question. Keep cost and latency low."
            ),
            "balanced": (
                "Several steps, useful planning, moderate code help, or a careful "
                "explanation. The task needs a capable general model."
            ),
            "strong": (
                "Non-trivial coding, debugging, research synthesis, architecture, or "
                "difficult tradeoffs. Use a strong reasoning model."
            ),
            "frontier": (
                "The hardest multi-step reasoning, high-stakes advice, broad research, "
                "or long-horizon implementation. Use the best available model."
            ),
        },
    },
    "task_shape": {
        "type": "choice",
        "instructions": "What is the primary shape of the user's request?",
        "criteria": {
            "direct": "A fact, definition, short calculation, or brief answer.",
            "explanation": "The user wants a concept or process explained.",
            "transformation": "The user wants text rewritten, summarized, translated, or formatted.",
            "implementation": "The user wants code written, changed, debugged, or tested.",
            "research": "The user wants current research, comparison, synthesis, or source-backed findings.",
            "decision": "The user wants help with a difficult choice, plan, or consequential recommendation.",
        },
    },
    "high_stakes": {
        "type": "noul",
        "instructions": (
            "Does this request ask for medical, legal, financial, security, safety, "
            "or another consequential judgment where a wrong answer could cause harm?"
        ),
        "criteria": {
            "true": "The request has meaningful real-world consequences.",
            "false": "The request is low stakes or ordinary information.",
        },
    },
}


@dataclass
class Decision:
    tier: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    task_shape: str | None = None
    high_stakes_probability: float | None = None
    source: str = "jev"
    target: dict[str, Any] | None = None
    alternates: list[dict[str, Any]] = field(default_factory=list)
    jev_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    reasons: list[str] = field(default_factory=list)
    promoted: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "confidence": round(float(self.confidence), 4),
            "probabilities": {k: round(float(v), 4) for k, v in self.probabilities.items()},
            "task_shape": self.task_shape,
            "high_stakes_probability": self.high_stakes_probability,
            "source": self.source,
            "target": self.target,
            "alternates": self.alternates[:5],
            "jev_model": self.jev_model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "reasons": self.reasons[:5],
            "promoted": self.promoted,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Decision":
        tier = str(payload.get("tier", "balanced"))
        if tier not in TIERS:
            tier = "balanced"
        probabilities = payload.get("probabilities", {})
        if not isinstance(probabilities, dict):
            probabilities = {}
        return cls(
            tier=tier,
            confidence=_number(payload.get("confidence"), 0.0),
            probabilities={str(k): _number(v, 0.0) for k, v in probabilities.items()},
            task_shape=str(payload["task_shape"]) if payload.get("task_shape") else None,
            high_stakes_probability=(
                _number(payload.get("high_stakes_probability"), 0.0)
                if payload.get("high_stakes_probability") is not None
                else None
            ),
            source=str(payload.get("source", "jev")),
            target=payload.get("target") if isinstance(payload.get("target"), dict) else None,
            alternates=[item for item in payload.get("alternates", []) if isinstance(item, dict)][:5],
            jev_model=str(payload["jev_model"]) if payload.get("jev_model") else None,
            input_tokens=_integer(payload.get("input_tokens")),
            output_tokens=_integer(payload.get("output_tokens")),
            latency_ms=_integer(payload.get("latency_ms")),
            reasons=[str(item) for item in payload.get("reasons", []) if item][:5],
            promoted=bool(payload.get("promoted", False)),
        )


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class JevError(RuntimeError):
    """The Jev request failed or returned an invalid response."""


class JevClient:
    """Small standard-library client for TypeSafe's documented HTTP API."""

    endpoint = "https://api.typesafe.ai/v1/systemone"

    def __init__(self, config: RouterConfig):
        self.config = config

    def evaluate(self, state: dict[str, Any]) -> dict[str, Any]:
        if not self.config.jev_api_key:
            raise JevError("JEV_API_KEY is not configured")
        body = json.dumps(
            {"state": state, "model": self.config.jev_model, "questions": ROUTER_QUESTIONS},
            separators=(",", ":"),
        ).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.config.jev_retries + 1):
            request = Request(
                self.endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.config.jev_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "hermes-jev-router/0.1",
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.config.jev_timeout) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
                if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), dict):
                    raise JevError("Jev returned an unexpected response")
                return parsed
            except HTTPError as error:
                last_error = error
                if error.code not in {429, 529} or attempt >= self.config.jev_retries:
                    detail = _safe_error_body(error)
                    raise JevError(f"Jev HTTP {error.code}: {detail}") from error
            except (URLError, TimeoutError, OSError, json.JSONDecodeError, JevError) as error:
                last_error = error
                if attempt >= self.config.jev_retries:
                    raise JevError(_safe_error_text(error)) from error
            time.sleep(0.15 * (3**attempt))
        raise JevError(_safe_error_text(last_error or RuntimeError("unknown Jev error")))


def _safe_error_body(error: HTTPError) -> str:
    try:
        body = error.read(512).decode("utf-8", errors="replace")
    except OSError:
        return "request failed"
    return " ".join(body.split())[:240] or "request failed"


def _safe_error_text(error: Exception) -> str:
    return " ".join(str(error).split())[:240] or error.__class__.__name__


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}:
                parts.append(str(part.get("text", "")))
        return " ".join(parts)
    return str(content or "")


def _history_digest(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    if not history:
        return []
    result: list[dict[str, str]] = []
    for message in history[-6:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", ""))
        if role not in {"user", "assistant"}:
            continue
        text = strip_markers(_message_text(message)).strip()
        if text:
            result.append({"role": role, "content": text[:1200]})
    return result


def _promote(tier: str, minimum: str) -> str:
    order = {name: index for index, name in enumerate(TIERS)}
    return TIERS[max(order.get(tier, 1), order.get(minimum, 1))]


def _parse_answer(response: dict[str, Any], key: str) -> dict[str, Any]:
    answer = response.get("answers", {}).get(key, {})
    return answer if isinstance(answer, dict) else {}


def classify_request(
    user_message: str,
    conversation_history: list[dict[str, Any]] | None = None,
    *,
    platform: str = "cli",
    model: str = "auto",
    tools_present: bool = False,
    config: RouterConfig | None = None,
    client: JevClient | None = None,
    available_models: list[dict[str, Any]] | None = None,
    route_choices: dict[str, dict[str, Any]] | None = None,
) -> Decision:
    """Ask Jev for one batched, typed route decision and apply safe promotions."""

    if config is None:
        from .config import load_config

        config = load_config()
    started = time.monotonic()
    state = {
        "request": user_message[:16000],
        "recent_conversation": _history_digest(conversation_history),
        "platform": platform,
        "current_model": model,
        "tools_available": tools_present,
    }
    if available_models:
        # The catalog builder caps discovery at 128. Keep the complete discovered
        # inventory in Jev's state so the route decision does not hide models.
        state["available_models"] = available_models[:128]
    if route_choices:
        state["route_choices"] = {
            tier: {key: value for key, value in choice.items() if key != "base_url"}
            for tier, choice in route_choices.items()
        }
    try:
        response = (client or JevClient(config)).evaluate(state)
        route = _parse_answer(response, "route_tier")
        shape = _parse_answer(response, "task_shape")
        stakes = _parse_answer(response, "high_stakes")
        tier = str(route.get("choice", config.default_tier))
        if tier not in TIERS:
            tier = config.default_tier
        probabilities = route.get("probabilities", {})
        if not isinstance(probabilities, dict):
            probabilities = {}
        confidence = max(0.0, min(1.0, _number(route.get("confidence"), 0.0)))
        task_shape = str(shape.get("choice")) if shape.get("choice") else None
        high_stakes = max(0.0, min(1.0, _number(stakes.get("noul"), 0.0)))
        reasons = [f"Jev selected {tier} for a {task_shape or 'general'} request"]
        promoted = False
        minimum = "micro"
        if confidence < config.min_confidence:
            minimum = "balanced"
            reasons.append(f"Low Jev confidence ({confidence:.0%}) raised the minimum to balanced")
        if task_shape in {"implementation", "research", "decision"} and tier in {"micro", "cheap"}:
            minimum = "balanced"
            reasons.append(f"The {task_shape} task shape needs more than a low-cost route")
        if high_stakes >= 0.5:
            minimum = "frontier"
            reasons.append(f"High-stakes signal was {high_stakes:.0%}; frontier route required")
        if len(user_message) > 10000:
            minimum = _promote(minimum, "balanced")
            reasons.append("Large request context raised the minimum to balanced")
        final_tier = _promote(tier, minimum)
        if final_tier != tier:
            promoted = True
            reasons.insert(0, f"Jev chose {tier}; policy promoted the route to {final_tier}")
        usage = response.get("usage", {}) if isinstance(response.get("usage"), dict) else {}
        target = route_choices.get(final_tier) if route_choices else None
        alternates = [
            {**choice, "tier": tier_name}
            for tier_name, choice in (route_choices or {}).items()
            if tier_name != final_tier and isinstance(choice, dict)
        ]
        return Decision(
            tier=final_tier,
            confidence=confidence,
            probabilities={str(k): _number(v, 0.0) for k, v in probabilities.items()},
            task_shape=task_shape,
            high_stakes_probability=high_stakes,
            source="jev",
            target=dict(target) if isinstance(target, dict) else None,
            alternates=alternates,
            jev_model=str(response.get("model")) if response.get("model") else config.jev_model,
            input_tokens=_integer(usage.get("input_tokens")),
            output_tokens=_integer(usage.get("output_tokens")),
            latency_ms=round((time.monotonic() - started) * 1000),
            reasons=reasons,
            promoted=promoted,
        )
    except Exception as error:
        reason = _safe_error_text(error)
        return Decision(
            tier=config.default_tier,
            confidence=0.0,
            source="fallback",
            jev_model=config.jev_model,
            latency_ms=round((time.monotonic() - started) * 1000),
            reasons=[f"Jev was unavailable ({reason}); used the {config.default_tier} default"],
            target=(dict(route_choices.get(config.default_tier)) if route_choices and isinstance(route_choices.get(config.default_tier), dict) else None),
            alternates=[
                {**choice, "tier": tier_name}
                for tier_name, choice in (route_choices or {}).items()
                if tier_name != config.default_tier and isinstance(choice, dict)
            ],
        )


def encode_marker(decision: Decision) -> str:
    return f"{MARKER_START}{json.dumps(decision.to_payload(), separators=(',', ':'))}{MARKER_END}"


def decode_marker(text: str) -> Decision | None:
    match = MARKER_RE.search(text or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return Decision.from_payload(payload) if isinstance(payload, dict) else None


def strip_markers(text: str) -> str:
    return MARKER_RE.sub("", text or "")


def strip_route_cards(text: str) -> str:
    """Remove prior visible decision cards from downstream conversation history."""

    return CARD_RE.sub("", text or "").strip()


def format_route_card(decision: Decision, target_model: str, *, platform: str = "cli") -> str:
    """Return a compact plain-text card that renders well in CLI and Telegram."""

    del platform
    tier_label = {
        "micro": "MICRO · instant",
        "cheap": "CHEAP · quick",
        "balanced": "BALANCED · capable",
        "strong": "STRONG · reasoning",
        "frontier": "FRONTIER · deep",
    }.get(decision.tier, decision.tier.upper())
    confidence = f"{decision.confidence:.0%}" if decision.source != "fallback" else "fallback"
    signals = [
        (tier, decision.probabilities[tier])
        for tier in TIERS
        if tier in decision.probabilities
    ]
    if not signals:
        signals = sorted(decision.probabilities.items(), key=lambda item: item[1], reverse=True)[:3]
    signal_text = " · ".join(f"{name} {value:.0%}" for name, value in signals) or "no probability data"
    reason = decision.reasons[0] if decision.reasons else "Jev evaluated the request"
    if len(reason) > 92:
        reason = reason[:89] + "..."
    lines = [
        CARD_START,
        f"│ Path: {tier_label}  ·  confidence {confidence}",
        f"│ Model: {target_model}",
        f"│ Why: {reason}",
        f"│ Jev signal: {signal_text}",
    ]
    if decision.task_shape:
        lines.append(f"│ Task shape: {decision.task_shape}")
    if decision.promoted:
        lines.append("│ Guardrail: route was promoted for safety or uncertainty")
    provenance = decision.jev_model or "not available"
    usage = ""
    if decision.input_tokens is not None:
        usage = f" · {decision.input_tokens} Jev in / {decision.output_tokens or 0} out"
    latency = f" · {decision.latency_ms} ms" if decision.latency_ms is not None else ""
    lines.append(f"│ Jev: {provenance}{usage}{latency}")
    lines.append(CARD_END)
    return "\n".join(lines)


# Created by Codex GPT-6 on 2026-09-17 11:03 PDT on ombee.
