"""Configuration for the portable Jev router.

All credentials are read from environment variables. Route metadata may be
provided as JSON in an environment variable or in a local file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any


TIERS = ("micro", "cheap", "balanced", "strong", "frontier")


@dataclass(frozen=True)
class RouteTarget:
    """One selectable downstream model."""

    tier: str
    model: str
    base_url: str
    api_key_env: str | None = None
    provider: str | None = None
    api_mode: str = "chat_completions"
    supports_tools: bool = True
    supports_streaming: bool = True
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, tier: str, value: dict[str, Any]) -> "RouteTarget":
        model = str(value.get("model", "")).strip()
        base_url = str(value.get("base_url", "")).strip()
        if not model:
            raise ValueError(f"Route {tier!r} has no model")
        if not base_url:
            raise ValueError(f"Route {tier!r} has no base_url")
        headers = value.get("headers", {})
        if not isinstance(headers, dict):
            raise ValueError(f"Route {tier!r} headers must be an object")
        return cls(
            tier=tier,
            model=model,
            base_url=base_url.rstrip("/"),
            api_key_env=str(value.get("api_key_env", "")).strip() or None,
            provider=str(value.get("provider", "")).strip() or None,
            api_mode=str(value.get("api_mode", "chat_completions")).strip() or "chat_completions",
            supports_tools=_bool_value(value.get("supports_tools", True), True),
            supports_streaming=_bool_value(value.get("supports_streaming", True), True),
            headers={str(k): str(v) for k, v in headers.items()},
        )


@dataclass(frozen=True)
class RouterConfig:
    """Resolved runtime settings."""

    jev_api_key: str | None
    jev_model: str = "jev-latest"
    jev_timeout: float = 4.0
    jev_retries: int = 2
    min_confidence: float = 0.45
    default_tier: str = "balanced"
    upstream_timeout: float = 300.0
    demo_mode: bool = False
    router_api_key: str | None = None
    routes: dict[str, RouteTarget] = field(default_factory=dict)


DEFAULT_ROUTE_CONFIG: dict[str, dict[str, Any]] = {
    tier: {
        "model": f"hermes/{tier}",
        "base_url": "demo://hermes-catalog",
        "provider": "demo",
        "supports_tools": True,
    }
    for tier in TIERS
}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bool_value(value: Any, default: bool = False) -> bool:
    """Parse booleans from JSON without treating ``\"false\"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _load_route_data() -> dict[str, dict[str, Any]]:
    inline = os.getenv("JEV_ROUTER_ROUTES_JSON", "").strip()
    file_name = os.getenv("JEV_ROUTER_ROUTES_FILE", "").strip()
    raw: Any = None
    if inline:
        raw = json.loads(inline)
    elif file_name:
        raw = json.loads(Path(file_name).expanduser().read_text(encoding="utf-8"))
    if raw is None:
        raw = DEFAULT_ROUTE_CONFIG
    if not isinstance(raw, dict):
        raise ValueError("JEV_ROUTER_ROUTES_JSON must contain an object")
    return {str(tier): value for tier, value in raw.items() if isinstance(value, dict)}


def load_config() -> RouterConfig:
    """Load and validate environment-backed router configuration."""

    raw_routes = _load_route_data()
    routes = {
        tier: RouteTarget.from_dict(tier, raw_routes[tier])
        for tier in TIERS
        if tier in raw_routes
    }
    # Read old three-tier route files without reintroducing an OpenRouter default.
    # The Hermes plugin supplies the new five-tier catalog at runtime.
    if routes and len(routes) < len(TIERS):
        for tier in TIERS:
            if tier not in routes:
                nearest = "balanced" if "balanced" in routes else next(iter(routes))
                source = routes[nearest]
                routes[tier] = RouteTarget(
                    tier=tier,
                    model=source.model,
                    base_url=source.base_url,
                    api_key_env=source.api_key_env,
                    provider=source.provider,
                    api_mode=source.api_mode,
                    supports_tools=source.supports_tools,
                    supports_streaming=source.supports_streaming,
                    headers=dict(source.headers),
                )
    routes = {tier: routes[tier] for tier in TIERS if tier in routes}
    default_tier = os.getenv("JEV_ROUTER_DEFAULT_TIER", "balanced").strip().lower()
    if default_tier not in TIERS:
        default_tier = "balanced"
    min_confidence = max(0.0, min(1.0, _float_env("JEV_ROUTER_MIN_CONFIDENCE", 0.45)))
    return RouterConfig(
        jev_api_key=os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY"),
        jev_model=os.getenv("JEV_ROUTER_JEV_MODEL", "jev-latest").strip() or "jev-latest",
        jev_timeout=max(0.5, _float_env("JEV_ROUTER_JEV_TIMEOUT", 4.0)),
        jev_retries=max(0, min(4, _int_env("JEV_ROUTER_JEV_RETRIES", 2))),
        min_confidence=min_confidence,
        default_tier=default_tier,
        upstream_timeout=max(1.0, _float_env("JEV_ROUTER_UPSTREAM_TIMEOUT", 300.0)),
        demo_mode=_bool_env("JEV_ROUTER_DEMO", False),
        router_api_key=os.getenv("JEV_ROUTER_API_KEY"),
        routes=routes,
    )


# Created by Codex GPT-6 on 2026-09-17 11:03 PDT on ombee.
