"""Hermes hook that performs the Jev decision before each model turn."""

from __future__ import annotations

from .catalog import route_choices_for_hermes
from .decision import classify_request, encode_marker, format_route_card


def _router_is_active(model: str) -> bool:
    """Use the configured provider when Hermes passes a resolved model name."""

    try:
        from hermes_cli.config import load_config

        configured = load_config().get("model", {})
        provider = str(configured.get("provider") or "").strip().lower()
        if provider in {"jev-router", "jev", "auto-router"}:
            return True
    except Exception:
        pass
    return (model or "auto").strip().lower() in {"auto", "jev-router", "jev", "auto-router"}


def register(ctx) -> None:
    """Register the per-turn Jev classifier and a manual preview command."""

    def on_pre_llm_call(
        user_message: str,
        conversation_history: list,
        is_first_turn: bool,
        model: str,
        platform: str,
        **kwargs,
    ):
        del is_first_turn, kwargs
        # A manually selected Hermes model must remain manual. Automatic routing
        # is active only for the router provider's virtual model.
        if not _router_is_active(model):
            return None
        available, choices = route_choices_for_hermes()
        decision = classify_request(
            user_message or "",
            conversation_history,
            platform=platform or "cli",
            model=model or "auto",
            available_models=[item.to_prompt() for item in available],
            route_choices=choices,
        )
        # The Jev provider strips this internal block before forwarding. It
        # lets the native provider and standalone sidecar share one marker.
        return {"context": encode_marker(decision)}

    ctx.register_hook("pre_llm_call", on_pre_llm_call)

    def preview(raw_args: str) -> str:
        text = (raw_args or "").strip()
        if not text:
            return "Usage: /jev <request to classify>"
        available, choices = route_choices_for_hermes()
        decision = classify_request(
            text,
            platform="cli",
            model="auto",
            available_models=[item.to_prompt() for item in available],
            route_choices=choices,
        )
        selected = decision.target or {}
        target = f"{selected.get('provider')}/{selected.get('model')}" if selected else decision.tier.upper() + " route"
        return format_route_card(decision, target)

    ctx.register_command(
        name="jev",
        handler=preview,
        description="Preview the Jev model route and the reasons for it.",
        args_hint="<request>",
    )


# Created by Codex GPT-6 on 2026-09-17 11:03 PDT on ombee.
