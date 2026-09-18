import unittest
from unittest.mock import patch

from hermes_jev_router.catalog import AvailableModel, build_five_point_routes
from hermes_jev_router.config import RouteTarget, RouterConfig, load_config
from hermes_jev_router.decision import (
    Decision,
    classify_request,
    decode_marker,
    encode_marker,
    format_route_card,
    strip_markers,
    strip_route_cards,
)


class FakeJev:
    def __init__(self, response):
        self.response = response
        self.state = None

    def evaluate(self, state):
        self.state = state
        return self.response


def config(**overrides):
    values = {
        "jev_api_key": "test-key",
        "routes": {},
    }
    values.update(overrides)
    return RouterConfig(**values)


class DecisionTests(unittest.TestCase):
    def test_five_route_slots_use_actual_models(self):
        models = [
            AvailableModel("provider", f"model-{index}", "https://models.test/v1", cost_input=index, cost_output=index)
            for index in range(7)
        ]
        routes = build_five_point_routes(models)
        self.assertEqual(list(routes), ["micro", "cheap", "balanced", "strong", "frontier"])
        self.assertEqual({route["provider"] for route in routes.values()}, {"provider"})
        self.assertEqual({route["base_url"] for route in routes.values()}, {"https://models.test/v1"})
        self.assertEqual(routes["micro"]["model"], "model-0")
        self.assertEqual(routes["frontier"]["model"], "model-6")

    def test_jev_receives_inventory_and_decision_carries_target(self):
        client = FakeJev(
            {
                "answers": {
                    "route_tier": {"choice": "strong", "confidence": 0.9},
                    "task_shape": {"choice": "implementation"},
                    "high_stakes": {"noul": 0.0},
                }
            }
        )
        routes = {tier: {"provider": "p", "model": tier, "base_url": "https://p.test/v1"} for tier in ("micro", "cheap", "balanced", "strong", "frontier")}
        inventory = [{"provider": "p", "model": str(index)} for index in range(45)]
        decision = classify_request(
            "Fix this code",
            config=config(),
            client=client,
            available_models=inventory,
            route_choices=routes,
        )
        self.assertEqual(decision.tier, "strong")
        self.assertEqual(decision.target["model"], "strong")
        self.assertEqual(len(client.state["available_models"]), 45)
        self.assertEqual(client.state["available_models"][-1]["model"], "44")
        self.assertNotIn("base_url", client.state["route_choices"]["strong"])

    def test_jev_choice_stays_cheap_for_direct_request(self):
        client = FakeJev(
            {
                "model": "jev-1.13.0",
                "answers": {
                    "route_tier": {"choice": "cheap", "confidence": 0.98, "probabilities": {"cheap": 0.98, "balanced": 0.02, "frontier": 0}},
                    "task_shape": {"choice": "direct"},
                    "high_stakes": {"noul": 0.01},
                },
                "usage": {"input_tokens": 10, "output_tokens": 4},
            }
        )
        decision = classify_request("What is the capital of France?", config=config(), client=client)
        self.assertEqual(decision.tier, "cheap")
        self.assertFalse(decision.promoted)
        self.assertEqual(client.state["request"], "What is the capital of France?")

    def test_jev_can_use_micro_for_a_confident_tiny_request(self):
        client = FakeJev(
            {
                "answers": {
                    "route_tier": {"choice": "micro", "confidence": 0.99, "probabilities": {"micro": 0.99, "cheap": 0.01}},
                    "task_shape": {"choice": "direct"},
                    "high_stakes": {"noul": 0.0},
                }
            }
        )
        decision = classify_request("Say hi.", config=config(), client=client)
        self.assertEqual(decision.tier, "micro")
        self.assertFalse(decision.promoted)

    def test_uncertainty_promotes_at_least_to_balanced(self):
        client = FakeJev(
            {
                "answers": {
                    "route_tier": {"choice": "cheap", "confidence": 0.2, "probabilities": {"cheap": 0.4, "balanced": 0.35, "frontier": 0.25}},
                    "task_shape": {"choice": "direct"},
                    "high_stakes": {"noul": 0.1},
                }
            }
        )
        decision = classify_request("Maybe explain this unclear request.", config=config(), client=client)
        self.assertEqual(decision.tier, "balanced")
        self.assertTrue(decision.promoted)

    def test_high_stakes_promotes_to_frontier(self):
        client = FakeJev(
            {
                "answers": {
                    "route_tier": {"choice": "balanced", "confidence": 0.8, "probabilities": {"cheap": 0.02, "balanced": 0.8, "frontier": 0.18}},
                    "task_shape": {"choice": "decision"},
                    "high_stakes": {"noul": 0.91},
                }
            }
        )
        decision = classify_request("Should I change my medication dose?", config=config(), client=client)
        self.assertEqual(decision.tier, "frontier")
        self.assertTrue(decision.promoted)

    def test_jev_failure_uses_balanced_default(self):
        class Broken:
            def evaluate(self, state):
                raise RuntimeError("network down")

        decision = classify_request("Hello", config=config(), client=Broken())
        self.assertEqual(decision.tier, "balanced")
        self.assertEqual(decision.source, "fallback")

    def test_marker_round_trip_and_removal(self):
        decision = Decision(
            tier="frontier",
            confidence=0.88,
            probabilities={"cheap": 0.01, "balanced": 0.11, "frontier": 0.88},
            task_shape="implementation",
            jev_model="jev-1.13.0",
            reasons=["Jev selected frontier"],
        )
        marked = "hello\n" + encode_marker(decision)
        decoded = decode_marker(marked)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.tier, "frontier")
        self.assertEqual(strip_markers(marked), "hello\n")

    def test_card_is_plain_text_and_explains_the_choice(self):
        card = format_route_card(
            Decision(
                tier="cheap",
                confidence=0.97,
                probabilities={"cheap": 0.97, "balanced": 0.02, "frontier": 0.01},
                task_shape="direct",
                jev_model="jev-1.13.0",
                reasons=["Jev selected cheap for a direct request"],
                latency_ms=113,
            ),
            "openai/gpt-5.4-mini",
        )
        self.assertIn("CHEAP", card)
        self.assertIn("openai/gpt-5.4-mini", card)
        self.assertIn("Jev signal", card)
        self.assertNotIn("**", card)

    def test_prior_card_can_be_removed_from_history(self):
        card = format_route_card(Decision(tier="cheap", confidence=1.0), "cheap-model")
        self.assertEqual(strip_route_cards(f"{card}\n\nreal answer"), "real answer")


class ConfigTests(unittest.TestCase):
    def test_old_three_tier_file_is_expanded_to_five(self):
        raw = {
            "cheap": {"model": "cheap-model", "base_url": "https://models.test/v1"},
            "balanced": {"model": "balanced-model", "base_url": "https://models.test/v1"},
            "frontier": {"model": "frontier-model", "base_url": "https://models.test/v1"},
        }
        with patch.dict("os.environ", {"JEV_ROUTER_ROUTES_JSON": __import__("json").dumps(raw)}, clear=False):
            loaded = load_config()
        self.assertEqual(tuple(loaded.routes), ("micro", "cheap", "balanced", "strong", "frontier"))
        self.assertEqual(loaded.routes["micro"].model, "balanced-model")
        self.assertEqual(loaded.routes["strong"].model, "balanced-model")

    def test_route_target_requires_model_and_endpoint(self):
        with self.assertRaises(ValueError):
            RouteTarget.from_dict("cheap", {})

    def test_string_false_is_not_treated_as_true(self):
        target = RouteTarget.from_dict(
            "cheap",
            {"model": "m", "base_url": "https://example.test", "supports_tools": "false", "supports_streaming": "0"},
        )
        self.assertFalse(target.supports_tools)
        self.assertFalse(target.supports_streaming)


# Created by Codex GPT-6 on 2026-09-17 11:03 PDT on ombee.
