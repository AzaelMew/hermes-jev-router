# Hermes Jev Router

Choose the right AI model for each request.

This plugin adds automatic model routing to [Hermes Agent](https://hermes-agent.nousresearch.com/). It uses [TypeSafe Jev](https://docs.typesafe.ai/) to judge the size and difficulty of each request. Hermes then sends the request to a suitable model.

The plugin is designed for people who use Hermes in a terminal or through Telegram. A small route card appears before each answer:

```text
╭─ ⚡ Jev model routing ─╮
│ Path: BALANCED · capable · confidence 92%
│ Model: your-hermes-provider/your-model
│ Why: Jev selected balanced for an explanation request
│ Jev signal: micro 1% · cheap 18% · balanced 77% · strong 4% · frontier 0%
│ Task shape: explanation
│ Jev: jev-1.13.0 · 310 ms
╰────────────────────────╯
```

## The important part: models are chosen on each machine

You do not give this plugin a fixed list of models.

When Hermes starts a routed turn, the plugin asks Hermes what providers and models are available on that machine. It uses Hermes' own credentials, model list, transport settings, prices, context limits, and tool support. It removes models that are not text models.

The plugin then gives the complete discovered catalog to Jev. Jev chooses one of five capability levels:

| Level | Use it for |
| --- | --- |
| `micro` | Tiny requests and very short answers |
| `cheap` | Simple answers and light transformations |
| `balanced` | Explanations, planning, and moderate code help |
| `strong` | Difficult coding, debugging, research, and trade-offs |
| `frontier` | The hardest or most consequential work |

The plugin maps the local catalog to five real Hermes targets. It does this at runtime. A different machine can produce a different five-model set. The examples in this README are placeholders. They are not required models.

If a selected target rejects a request because of authentication, quota, or model availability, the router tries another discovered Hermes target. The route card explains the fallback.

Hermes can list a model without proving that the provider will accept every live request. The router keeps the turn alive when that happens.

## What you need

You need:

- Hermes Agent installed and working.
- At least one model provider authenticated in Hermes.
- A TypeSafe Jev API key.
- Python 3.10 or newer.

You do not need OpenRouter. OpenRouter is not a default or hidden route.

## Setup

### 1. Download the plugin

```bash
git clone https://github.com/jethrojones/hermes-jev-router.git
cd hermes-jev-router
```

### 2. Make sure Hermes knows your models

Authenticate the providers that you want Hermes to use before you start the router. Use the normal Hermes setup flow. The plugin discovers the models from that Hermes installation.

Do not copy a model list from this repository. Do not copy another user's provider settings. Your local Hermes catalog is the source of truth.

### 3. Install the plugin

```bash
python3 scripts/install_hermes.py
```

The installer copies plugin code and manifests into `~/.hermes/plugins/`. It does not copy environment files or credentials.

### 4. Add the Jev key

Put your TypeSafe key in the environment that starts Hermes:

```bash
JEV_API_KEY=jev_your_key_here
```

You can place this line in the private Hermes environment file used by your installation. Never put the real key in this repository.

Get a key from the [TypeSafe console](https://console.typesafe.ai/).

### 5. Enable automatic routing

Add or update the Hermes configuration:

```yaml
model:
  provider: jev-router
  default: auto
  base_url: http://127.0.0.1:8765/v1

plugins:
  enabled:
    - jev-router-inline
```

The `auto` model activates routing. Selecting a normal Hermes model by hand still uses that model directly.

Restart Hermes after you change the configuration.

### 6. Check the installation

```bash
hermes plugins doctor ~/.hermes/plugins/jev-router-inline --ci
hermes -z "What is the capital of France? Reply with only the city name."
```

You must see the Jev route card before the answer.

## Try the five levels

Run these one at a time:

```bash
hermes -z "Say hello in one word."
hermes -z "What is the capital of France? Reply with only the city name."
hermes -z "Explain a three-step SQLite to Postgres migration."
hermes -z "Explain how to design a retryable HTTP client and include a short code sketch."
hermes -z "Design a production-safe multi-tenant job workflow with retries and rollback."
```

The route can vary. Jev may promote a request when it is uncertain or when the request needs more care. The exact provider and model come from your Hermes catalog.

## Telegram

No separate Telegram bot is required. Hermes uses its normal Telegram gateway.

Send the same prompts from an allowed Telegram account. The route card appears before the answer. It uses plain text so Telegram does not misread Markdown. It also works with streaming and non-streaming Hermes turns.

## How the choice is made

For each routed turn:

1. Hermes reports its available model catalog.
2. The plugin removes router providers and non-text models.
3. The plugin sends the complete model summaries to Jev. It does not send credentials.
4. Jev classifies the request into one of five levels.
5. The plugin maps that level to one real model from the current Hermes catalog.
6. Hermes sends the clean request to that model.

The route card shows the level, model, Jev probabilities, task shape, confidence, and reason. It also shows when a safety rule promoted the route or when a fallback was needed.

## Credentials and privacy

Jev receives the current request and a short recent-conversation summary. Do not route private data to TypeSafe unless your policy allows it.

Jev does not receive provider keys. Hermes keeps provider authentication and sends the final request to the selected model.

This repository contains no working API key. The example files contain placeholders only.

## Local demo mode

You can test the route card without calling a real answer model. This still calls Jev for the routing decision.

```bash
export JEV_API_KEY=jev_your_key_here
export JEV_ROUTER_API_KEY=local-router
export JEV_ROUTER_DEMO=1
PYTHONPATH=. python3 scripts/run_router.py
```

The local service listens on `http://127.0.0.1:8765`.

```bash
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8765/v1/models
```

Demo mode returns a local rehearsal response. It does not call the selected answer model.

Set `JEV_ROUTER_DEMO=0` for live downstream answers. Hermes native mode uses the provider credentials already configured in Hermes.

## Standalone sidecar

The repository also contains a small OpenAI-compatible sidecar. It is useful for clients that cannot load a Hermes plugin.

Hermes native mode does not need the sidecar on current Hermes versions. An older Hermes version can use the sidecar as a compatibility bridge.

The example route file is only for the standalone sidecar or OpenClaw. It is not the Hermes model catalog:

```bash
cp config/routes.example.json /tmp/hermes-jev-routes.json
export JEV_ROUTER_ROUTES_FILE=/tmp/hermes-jev-routes.json
```

Keep real keys in environment variables. Never put them in the route file.

## OpenClaw

OpenClaw has a native plugin in the `openclaw/` directory. It uses OpenClaw's `before_model_resolve` hook. OpenClaw still owns the model call, tools, credentials, and failover.

The OpenClaw plugin discovers models with OpenClaw's own model list command. It uses only models that OpenClaw reports as available. It excludes OpenRouter by default. It does not use the model list from this README or from the author's machine.

The plugin needs the local sidecar for the Jev decision. Start the sidecar with the same private environment that contains your Jev key:

```bash
export JEV_API_KEY=jev_your_key_here
export JEV_ROUTER_API_KEY=local-router
PYTHONPATH=. python3 scripts/run_router.py
```

Install the OpenClaw plugin from this repository:

```bash
openclaw plugins install --link ./openclaw --force
openclaw plugins enable hermes-jev-router
```

Add this entry to `~/.openclaw/openclaw.json`:

```json5
{
  plugins: {
    entries: {
      "hermes-jev-router": {
        enabled: true,
        hooks: { allowConversationAccess: true },
        config: {
          showRouteCard: true,
          respectManualModel: false,
          allowOpenRouter: false
        }
      }
    }
  }
}
```

Then reload the plugin or restart the Gateway:

```bash
openclaw plugins reload hermes-jev-router
openclaw plugins inspect hermes-jev-router --runtime --json
```

The plugin sends the current prompt and safe model facts to the sidecar. Jev selects one of five levels. The plugin returns that level's real `provider/model` pair to OpenClaw. The reply starts with the same route card used by Hermes and Telegram.

If the sidecar or Jev is unavailable, OpenClaw keeps its normal model. The plugin does not block the conversation.

To keep a manually selected model, set `respectManualModel` to `true`. With the default `false`, the plugin routes every normal OpenClaw turn. This is the automatic mode.

## Troubleshooting

### I do not see the route card

Check that Hermes uses `provider: jev-router` and `default: auto`. Check that `jev-router-inline` is enabled. Then restart Hermes.

### Jev is unavailable

The router uses the configured `balanced` default and says so in the card. Check `JEV_API_KEY` and network access.

### A model fails after Jev selects it

The router tries another discovered Hermes target for authentication, quota, and unsupported-model errors. Check the provider's Hermes authentication if this repeats.

### The available models differ from this README

That is expected. The plugin uses the models available in your Hermes installation. It does not use the author's model list.

## Tests for maintainers

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q hermes_jev_router providers scripts
node --test openclaw/catalog.test.mjs
node --check openclaw/index.js
git diff --check
```

The tests cover five-level routing, full catalog handoff, native Hermes dispatch, streaming, provider failover, OpenClaw catalog discovery, and Telegram-safe route cards.

## Sources

- [TypeSafe API](https://docs.typesafe.ai/api)
- [TypeSafe Choice](https://docs.typesafe.ai/primitives/choice)
- [TypeSafe intent routing](https://docs.typesafe.ai/patterns/intent-routing)
- [Hermes plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)
- [Hermes hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)
- [Hermes model provider plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/model-provider-plugin)
- [OpenClaw custom providers](https://docs.openclaw.ai/concepts/model-providers/custom-providers)
- [OpenClaw plugin hooks](https://docs.openclaw.ai/plugins/hooks)
- [OpenClaw hook reference](https://docs.openclaw.ai/plugins/hooks/reference)

## License

MIT. See [LICENSE](LICENSE).

Created by Codex GPT-6 on 2026-09-18 12:00 PDT on ombee.
