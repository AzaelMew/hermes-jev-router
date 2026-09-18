"""Hermes model-provider profile for the native Jev router."""

import os

from providers import register_provider
from providers.base import ProviderProfile
from .router_client import JevRouterClient


class JevRouterProfile(ProviderProfile):
    """Virtual provider that dispatches each turn to a discovered Hermes target."""

    def create_client(self, **client_kwargs):
        return JevRouterClient(dict(client_kwargs))


register_provider(
    JevRouterProfile(
        name="jev-router",
        aliases=("jev", "auto-router"),
        display_name="Jev Router",
        description="Explainable automatic model routing through TypeSafe Jev",
        signup_url="https://console.typesafe.ai",
        env_vars=("JEV_ROUTER_API_KEY", "JEV_ROUTER_BASE_URL"),
        base_url=os.getenv("JEV_ROUTER_BASE_URL", "http://127.0.0.1:8765/v1"),
        auth_type="api_key",
        fallback_models=("auto", "micro", "cheap", "balanced", "strong", "frontier"),
        default_aux_model="micro",
    )
)


# Created by Codex GPT-6 on 2026-09-17 11:03 PDT on ombee.
