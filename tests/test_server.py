import unittest

from hermes_jev_router.config import RouteTarget, RouterConfig
from hermes_jev_router.decision import Decision, encode_marker, format_route_card
from hermes_jev_router.server import clean_messages, _decision_from_messages, _requested_tier, _route_target


class ServerTests(unittest.TestCase):
    def test_clean_messages_removes_internal_decision(self):
        marker = encode_marker(Decision(tier="cheap", confidence=1.0))
        messages = [{"role": "user", "content": "Hello\n" + marker}]
        cleaned = clean_messages(messages)
        self.assertEqual(cleaned[0]["content"], "Hello")

    def test_clean_messages_removes_prior_visible_card(self):
        card = format_route_card(Decision(tier="cheap", confidence=1.0), "cheap-model")
        messages = [{"role": "assistant", "content": f"{card}\n\nanswer"}]
        cleaned = clean_messages(messages)
        self.assertEqual(cleaned[0]["content"], "answer")

    def test_sidecar_can_reuse_the_hook_decision(self):
        marker = encode_marker(Decision(tier="frontier", confidence=0.8))
        messages = [{"role": "user", "content": marker}]
        decision = _decision_from_messages(messages)
        self.assertEqual(decision.tier, "frontier")

    def test_model_aliases(self):
        self.assertEqual(_requested_tier("jev-router/micro"), "micro")
        self.assertEqual(_requested_tier("jev-router/frontier"), "frontier")
        self.assertEqual(_requested_tier("strong"), "strong")
        self.assertEqual(_requested_tier("balanced"), "balanced")
        self.assertIsNone(_requested_tier("auto"))

    def test_decision_target_wins_over_static_sidecar_table(self):
        static = RouteTarget("strong", "static-model", "https://static.test/v1")
        config = RouterConfig(jev_api_key="test", routes={tier: static for tier in ("micro", "cheap", "balanced", "strong", "frontier")})
        decision = Decision(
            tier="strong",
            confidence=1.0,
            target={"provider": "openai-codex", "model": "real-model", "base_url": "https://real.test/v1"},
        )
        _, target, error = _route_target(config, decision, False)
        self.assertIsNone(error)
        self.assertEqual(target.model, "real-model")


# Created by Codex GPT-6 on 2026-09-17 11:03 PDT on ombee.
