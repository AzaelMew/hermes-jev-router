import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


CLIENT_PATH = Path(__file__).parents[1] / "providers" / "jev-router" / "router_client.py"
SPEC = importlib.util.spec_from_file_location("jev_router_test_client", CLIENT_PATH)
router_client = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = router_client
SPEC.loader.exec_module(router_client)


def marker(alternates=None, **target_overrides):
    target = {
        "provider": "custom",
        "model": "real-model",
        "base_url": "https://models.test/v1",
        "api_mode": "chat_completions",
        "supports_tools": True,
        "supports_streaming": True,
    }
    target.update(target_overrides)
    payload = {
        "tier": "strong",
        "confidence": 0.91,
        "probabilities": {"strong": 0.91, "frontier": 0.09},
        "task_shape": "implementation",
        "jev_model": "jev-test",
        "reasons": ["Jev selected strong for an implementation request"],
        "target": target,
    }
    if alternates is not None:
        payload["alternates"] = alternates
    return "[JEV_ROUTER_DECISION_V1]" + json.dumps(payload) + "[/JEV_ROUTER_DECISION_V1]"


class FakeCompletions:
    def __init__(self):
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        if request.get("stream"):
            return iter([
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="real "), finish_reason=None)]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="answer"), finish_reason=None)]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=""), finish_reason="stop")]),
            ])
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(role="assistant", content="real answer", tool_calls=None),
                finish_reason="stop",
            )],
            model=request["model"],
        )


class ProviderClientTests(unittest.TestCase):
    def test_route_level_provider_errors_are_failoverable(self):
        self.assertTrue(router_client._should_failover(RuntimeError("HTTP 401: API key is invalid")))
        self.assertTrue(router_client._should_failover(RuntimeError("HTTP 400: model_not_supported")))
        self.assertTrue(router_client._should_failover(RuntimeError("HTTP 429: The usage limit has been reached")))
        self.assertTrue(router_client._should_failover(RuntimeError("HTTP 429: Rate limit reached, retry later")))
        self.assertFalse(router_client._should_failover(RuntimeError("HTTP 400: invalid prompt")))

    def test_native_client_resolves_marker_target_and_strips_marker(self):
        fake = FakeCompletions()
        downstream = SimpleNamespace(chat=SimpleNamespace(completions=fake))
        runtime_module = ModuleType("hermes_cli.runtime_provider")
        runtime_module.resolve_runtime_provider = lambda **kwargs: {
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": "https://models.test/v1",
            "api_key": "runtime-secret",
        }
        messages = [{"role": "user", "content": "Fix this\n" + marker()}]
        client = router_client.JevRouterClient({})
        with patch.dict(sys.modules, {"hermes_cli.runtime_provider": runtime_module}), patch.object(
            router_client, "_downstream_client", return_value=downstream
        ):
            response = client.chat.completions.create(model="auto", messages=messages)
        self.assertEqual(fake.requests[0]["model"], "real-model")
        self.assertNotIn("JEV_ROUTER_DECISION", fake.requests[0]["messages"][0]["content"])
        self.assertIn("⚡ Jev · STRONG", response.choices[0].message.content)
        self.assertIn("real answer", response.choices[0].message.content)

    def test_native_client_emits_a_complete_stream_with_route_card(self):
        fake = FakeCompletions()
        downstream = SimpleNamespace(chat=SimpleNamespace(completions=fake))
        runtime_module = ModuleType("hermes_cli.runtime_provider")
        runtime_module.resolve_runtime_provider = lambda **kwargs: {
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": "https://models.test/v1",
            "api_key": "runtime-secret",
        }
        client = router_client.JevRouterClient({})
        with patch.dict(sys.modules, {"hermes_cli.runtime_provider": runtime_module}), patch.object(
            router_client, "_downstream_client", return_value=downstream
        ):
            chunks = list(client.chat.completions.create(
                model="auto", stream=True, messages=[{"role": "user", "content": marker()}]
            ))
        self.assertGreaterEqual(len(chunks), 3)
        self.assertIn("⚡ Jev · STRONG", chunks[0].choices[0].delta.content)
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")

    def test_invalid_target_fails_closed(self):
        client = router_client.JevRouterClient({})
        with self.assertRaises(RuntimeError):
            client.chat.completions.create(
                model="auto", messages=[{"role": "user", "content": marker(base_url="file:///tmp")}]
            )

    def test_auth_failure_fails_over_to_next_hermes_target(self):
        first = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **request: (_ for _ in ()).throw(RuntimeError("HTTP 401: API key is invalid"))
                )
            )
        )
        second_fake = FakeCompletions()
        second = SimpleNamespace(chat=SimpleNamespace(completions=second_fake))
        runtime_module = ModuleType("hermes_cli.runtime_provider")
        runtime_module.resolve_runtime_provider = lambda **kwargs: {
            "provider": kwargs["requested"],
            "api_mode": "chat_completions",
            "base_url": kwargs["explicit_base_url"],
            "api_key": "runtime-secret",
        }
        alternate = {
            "tier": "balanced",
            "provider": "backup",
            "model": "backup-model",
            "base_url": "https://backup.test/v1",
            "api_mode": "chat_completions",
            "supports_tools": True,
            "supports_streaming": True,
        }
        downstreams = iter([first, second])
        client = router_client.JevRouterClient({})
        with patch.dict(sys.modules, {"hermes_cli.runtime_provider": runtime_module}), patch.object(
            router_client, "_downstream_client", side_effect=lambda *args: next(downstreams)
        ):
            response = client.chat.completions.create(
                model="auto",
                messages=[{"role": "user", "content": marker(alternates=[alternate])}],
            )
        self.assertEqual(second_fake.requests[0]["model"], "backup-model")
        self.assertIn("⚡ Jev · BALANCED · backup-model · medium · fallback", response.choices[0].message.content)


# Created by Codex GPT-6 on 2026-09-17 16:22 PDT on ombee.
