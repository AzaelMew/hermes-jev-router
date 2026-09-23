"""Discover and rank the models that the local Hermes instance can use.

The catalog is read inside the Hermes process. It uses Hermes' own inventory and
runtime resolver, so a model is not offered to Jev unless Hermes can resolve it.
No credential value leaves this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import re
import threading
import time
from typing import Any


logger = logging.getLogger(__name__)

ROUTER_PROVIDER_NAMES = frozenset({"jev-router", "jev", "auto-router"})
CATALOG_TTL_SECONDS = 60.0
NON_TEXT_MODEL_RE = re.compile(r"(?:embedding|moderation|tts|transcri|image|video|audio)", re.IGNORECASE)


@dataclass(frozen=True)
class AvailableModel:
    """A resolved Hermes model route without any credential material."""

    provider: str
    model: str
    base_url: str
    api_mode: str = "chat_completions"
    supports_tools: bool = True
    supports_streaming: bool = True
    reasoning: bool = True
    reasoning_effort: str | None = None
    context_window: int = 0
    cost_input: float | None = None
    cost_output: float | None = None
    label: str = ""
    source: str = "hermes"

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def display_name(self) -> str:
        return f"{self.provider}/{self.model}"

    def to_prompt(self) -> dict[str, Any]:
        """Safe model facts sent to Jev for route selection."""

        result: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "reasoning": self.reasoning,
        }
        if self.context_window:
            result["context_window"] = self.context_window
        if self.cost_input is not None or self.cost_output is not None:
            result["cost_per_million_tokens"] = {
                "input": self.cost_input,
                "output": self.cost_output,
            }
        return result

    def to_target(self) -> dict[str, Any]:
        """Route metadata for the local provider bridge and the standalone sidecar."""

        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_mode": self.api_mode,
            "reasoning_effort": self.reasoning_effort,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
        }


_catalog_lock = threading.Lock()
_catalog_cache: tuple[float, tuple[AvailableModel, ...]] = (0.0, ())


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
        return number if number > 0 else 0
    except (TypeError, ValueError):
        return 0


def _runtime_for(provider: str, model: str) -> dict[str, Any] | None:
    """Resolve one model through Hermes without returning its credential."""

    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=provider, target_model=model)
    except Exception:
        return None
    if not isinstance(runtime, dict):
        return None
    base_url = str(runtime.get("base_url") or "").strip()
    if not base_url or base_url.lower().startswith(("acp:", "bedrock:", "moa:")):
        return None
    resolved_provider = str(runtime.get("provider") or provider).strip().lower()
    if resolved_provider in ROUTER_PROVIDER_NAMES or resolved_provider == "openrouter":
        # OpenRouter is opt-in. The router must not silently turn an available
        # Hermes subscription or local endpoint into an OpenRouter bill.
        if resolved_provider == "openrouter" and os.environ.get("JEV_ROUTER_ALLOW_OPENROUTER", "") not in {
            "1", "true", "yes", "on"
        }:
            return None
        if resolved_provider in ROUTER_PROVIDER_NAMES:
            return None
    return {
        "provider": resolved_provider,
        "base_url": base_url,
        "api_mode": str(runtime.get("api_mode") or "chat_completions"),
    }


def _model_info(provider: str, model: str) -> dict[str, Any]:
    try:
        from agent.models_dev import get_model_info

        info = get_model_info(provider, model, allow_network=False)
    except Exception:
        info = None
    if info is None:
        return {}
    return {
        "reasoning": bool(getattr(info, "reasoning", True)),
        "supports_tools": bool(getattr(info, "tool_call", True)),
        "input_modalities": tuple(getattr(info, "input_modalities", ()) or ()),
        "output_modalities": tuple(getattr(info, "output_modalities", ()) or ()),
        "context_window": _positive_int(getattr(info, "context_window", 0)),
        "cost_input": _number(getattr(info, "cost_input", None)),
        "cost_output": _number(getattr(info, "cost_output", None)),
    }


def _inventory_models() -> list[AvailableModel]:
    """Read Hermes' authenticated model inventory using cache-friendly options."""

    from hermes_cli.inventory import build_models_payload, load_picker_context

    context = load_picker_context()
    payload = build_models_payload(
        context,
        explicit_only=False,
        include_unconfigured=False,
        picker_hints=False,
        canonical_order=True,
        pricing=False,
        capabilities=True,
        refresh=False,
        probe_custom_providers=False,
        probe_current_custom_provider=False,
        max_models=128,
    )
    models: list[AvailableModel] = []
    seen: set[str] = set()
    runtime_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    for row in payload.get("providers", []):
        if not isinstance(row, dict):
            continue
        provider = str(row.get("slug") or "").strip().lower()
        if not provider or provider in ROUTER_PROVIDER_NAMES:
            continue
        row_models = row.get("models") or []
        capabilities = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
        for raw_model in row_models:
            model = str(raw_model or "").strip()
            if not model:
                continue
            # Most Hermes providers use one runtime for all text models. Keep
            # the resolver from refreshing the same OAuth credential once per
            # catalog row. OpenCode can change API mode by model, so it keeps
            # a model-specific cache key.
            cache_key = (provider, model) if provider.startswith("opencode") else (provider, "")
            if cache_key not in runtime_cache:
                runtime_cache[cache_key] = _runtime_for(provider, model)
            runtime = runtime_cache[cache_key]
            if runtime is None:
                continue
            info = _model_info(runtime["provider"], model)
            output_modalities = info.get("output_modalities", ())
            if NON_TEXT_MODEL_RE.search(model) or (output_modalities and "text" not in output_modalities):
                continue
            identity = f"{runtime['provider']}:{model}"
            if identity in seen:
                continue
            seen.add(identity)
            capability = capabilities.get(model) if isinstance(capabilities, dict) else {}
            capability = capability if isinstance(capability, dict) else {}
            models.append(
                AvailableModel(
                    provider=runtime["provider"],
                    model=model,
                    base_url=runtime["base_url"],
                    api_mode=runtime["api_mode"],
                    supports_tools=bool(info.get("supports_tools", capability.get("tools", capability.get("tool_call", True)))),
                    supports_streaming=True,
                    reasoning=bool(info.get("reasoning", capability.get("reasoning", True))),
                    context_window=_positive_int(info.get("context_window")),
                    cost_input=info.get("cost_input"),
                    cost_output=info.get("cost_output"),
                    label=str(row.get("name") or provider),
                    source=str(row.get("source") or "hermes"),
                )
            )
    return models


def discover_hermes_models(*, force: bool = False) -> list[AvailableModel]:
    """Return a short-lived, credential-free snapshot of usable Hermes routes."""

    global _catalog_cache
    now = time.monotonic()
    with _catalog_lock:
        cached_at, cached = _catalog_cache
        if not force and cached and now - cached_at < CATALOG_TTL_SECONDS:
            return list(cached)
        try:
            fresh = tuple(_inventory_models())
        except Exception:
            logger.debug("Hermes model discovery failed", exc_info=True)
            fresh = ()
        if fresh:
            _catalog_cache = (now, fresh)
            return list(fresh)
        return list(cached)


def _quality(model: AvailableModel) -> float:
    """Stable capability estimate used only to spread five route slots."""

    name = model.model.lower()
    score = 1.0
    if model.reasoning:
        score += 1.0
    if model.context_window >= 200_000:
        score += 1.0
    if model.context_window >= 1_000_000:
        score += 0.5
    for token, points in (
        ("nano", -1.2), ("mini", -0.9), ("flash", -0.5), ("haiku", -0.5),
        ("small", -0.6), ("lite", -0.8), ("fast", -0.3),
        ("pro", 1.0), ("max", 1.0), ("opus", 1.2), ("ultra", 1.0),
        ("sol", 1.0), ("terra", 0.6), ("astra", 1.1),
    ):
        if token in name:
            score += points
    return score


def _cost(model: AvailableModel) -> float:
    if model.cost_input is not None or model.cost_output is not None:
        return (model.cost_input or 0.0) + (model.cost_output or 0.0)
    return max(0.05, _quality(model))


def build_five_point_routes(models: list[AvailableModel]) -> dict[str, dict[str, Any]]:
    """Spread the available Hermes models across five capability levels."""

    if not models:
        return {}
    eligible = [item for item in models if item.supports_tools] or list(models)
    by_cost = sorted(eligible, key=lambda item: (_cost(item), _quality(item), item.display_name))
    by_quality = sorted(eligible, key=lambda item: (_quality(item), _cost(item), item.display_name))
    picks: list[AvailableModel] = []
    for fraction, source in (
        (0.0, by_cost),
        (0.25, by_cost),
        (0.5, by_quality),
        (0.75, by_quality),
        (1.0, by_quality),
    ):
        index = round(fraction * (len(source) - 1))
        candidate = source[index]
        if candidate in picks:
            candidate = next((item for item in source if item not in picks), candidate)
        picks.append(candidate)
    while len(picks) < 5:
        picks.append(picks[-1])
    return {
        tier: picks[index].to_target()
        for index, tier in enumerate(("micro", "cheap", "balanced", "strong", "frontier"))
    }


def route_choices_for_hermes(*, force: bool = False) -> tuple[list[AvailableModel], dict[str, dict[str, Any]]]:
    models = discover_hermes_models(force=force)
    return models, build_five_point_routes(models)


# Created by Codex GPT-6 on 2026-09-17 15:42 PDT on ombee.
