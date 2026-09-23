# Hermes Jev Router

Choose the right AI model for each request.

This plugin adds automatic model routing to [Hermes Agent](https://hermes-agent.nousresearch.com/). It uses [TypeSafe Jev](https://docs.typesafe.ai/) to judge the size and difficulty of each request. Hermes then sends the request to a suitable model.

The Jev router works across Hermes interfaces, including the app, terminal, Telegram, and Discord. It adds a compact route label before each answer:

```text
⚡ Jev · BALANCED · 6-luna · medium
```

When a route uses a backup model, the label ends with `· fallback`.

## The important part: models are chosen on each machine

By default, the native plugin discovers up to 128 models that Hermes can resolve locally and maps them to five tiers. Explicit route configuration supplied through `JEV_ROUTER_ROUTES_FILE` or `JEV_ROUTER_ROUTES_JSON` overrides discovery. Catalog discovery does not prove a provider will accept a live request.

| Level | Use it for |
| --- | --- |
| `micro` | Tiny requests and very short answers |
| `cheap` | Simple answers and light transformations |
| `balanced` | Explanations, planning, and moderate code help |
| `strong` | Difficult coding, debugging, research, and trade-offs |
| `frontier` | The hardest or most consequential work |

The plugin maps the local catalog to five real Hermes targets. It does this at runtime. A different machine can produce a different five-model set. The examples in this README are placeholders. They are not required models.

You can pin tier models and reasoning effort in a route file referenced by `JEV_ROUTER_ROUTES_FILE`, or provide routes inline with `JEV_ROUTER_ROUTES_JSON`. The native plugin does not automatically read `~/.hermes/jev-routes.json` unless you point `JEV_ROUTER_ROUTES_FILE` to it. The five tiers default to `minimal`, `low`, `medium`, `high`, and `xhigh` reasoning effort respectively. Providers may map these settings differently or reject unsupported values.

A route can also declare a `fallback` target. In native Hermes mode, the router tries it after a failoverable primary error (for example, an authentication, quota, or unavailable-model error) and before trying another tier's route:

```json
{
  "cheap": {
    "provider": "kimi-coding",
    "model": "kimi-for-coding",
    "base_url": "https://api.kimi.com/coding",
    "api_mode": "anthropic_messages",
    "fallback": {
      "provider": "openai-codex",
      "model": "gpt-6-luna",
      "base_url": "https://chatgpt.com/backend-api/codex",
      "api_mode": "codex_responses",
      "reasoning_effort": "low"
    }
  }
}
```

Routes are read per request, so changes to `jev-routes.json` take effect without restarting Hermes. Restart Hermes after installing or changing plugin code so it loads the updated plugin.

For native Hermes dispatch, recognized authentication, quota, guardrail, and unavailable-model errors trigger a configured per-tier fallback and then the decision's alternate targets, when present. Other errors or exhausted routes fail the request. The standalone sidecar's HTTP-forwarding path does not provide this same downstream failover.

## What you need

You need:

- Hermes Agent installed and working.
- At least one model provider authenticated in Hermes.
- A Jev decision credential: `JEV_API_KEY`/`TYPESAFE_API_KEY`, or `OPENROUTER_API_KEY` for the compatible Decisions endpoint.
- For native Hermes provider admission, a non-empty `JEV_ROUTER_API_KEY` setting (the placeholder value is not used as an upstream credential).
- Python 3.10 or newer.

You do not need OpenRouter. It is only used if you choose it for Jev decisions or explicitly opt in to OpenRouter answer models.

## Setup

### 1. Download the plugin

```bash
git clone https://github.com/AzaelMew/hermes-jev-router.git
cd hermes-jev-router
```

### 2. Make sure Hermes knows your models

Authenticate the providers that you want Hermes to use before you start the router. Use the normal Hermes setup flow. The plugin discovers the models from that Hermes installation.

Do not copy a model list from this repository. Do not copy another user's provider settings. Your local Hermes catalog is the source of truth.

### 3. Install the plugin

```bash
python3 scripts/install_hermes.py
```

The installer copies plugin code and manifests into `~/.hermes/plugins/`. It installs both the native Hermes hook and the model-provider adapter; it does not copy environment files or credentials.

### 4. Add a Jev decision key

For direct TypeSafe decisions, set `JEV_API_KEY` or `TYPESAFE_API_KEY`. Alternatively, set `OPENROUTER_API_KEY` to use OpenRouter's compatible Decisions endpoint. These keys are for Jev's routing decision, not the downstream answer model.

```bash
JEV_API_KEY=jev_your_key_here
# Or, instead:
# OPENROUTER_API_KEY=your_openrouter_key
```

Store the key in the private environment used to start Hermes; do not commit it. Get a TypeSafe key from the [TypeSafe console](https://console.typesafe.ai/).

For native Hermes model-provider admission, also set `JEV_ROUTER_API_KEY` to any non-empty placeholder, for example `local-router`. The provider requires this setting to be present; the value is not used to authenticate downstream model calls. For native Hermes model discovery, OpenRouter answer models require `JEV_ROUTER_ALLOW_OPENROUTER=1`; explicitly pinned OpenRouter route targets are a separate opt-in through the route configuration itself.

### 5. Enable automatic routing

Add or update the Hermes configuration:

```yaml
model:
  provider: jev-router
  default: auto
  base_url: http://127.0.0.1:8765/v1 # virtual provider URL; native mode does not require a local server

plugins:
  enabled:
    - jev-router-inline
```

The `auto` model activates routing. Manual model selection bypasses the hook when you switch away from the `jev-router` provider; selecting a different model name while that provider remains active does not disable routing. Native Hermes mode dispatches through the installed provider adapter and does **not** require the sidecar process.

Restart Hermes after changing the provider/plugin configuration so it loads the updated settings. Changes to `~/.hermes/jev-routes.json` are read per request and do not require a restart.

### 6. Check the installation

```bash
hermes plugins doctor ~/.hermes/plugins/jev-router-inline --ci
hermes -z "What is the capital of France? Reply with only the city name."
```

A successful routed answer should begin with the compact Jev label. Plugin doctor checks that the hook is registered; it does not prove a live Jev decision or downstream model call.

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

Send the same prompts from an allowed Telegram account. The compact Jev route label appears before the answer as plain text, so Telegram does not misread it as Markdown. It also works with streaming and non-streaming Hermes turns.

## How the choice is made

For each routed turn:

1. Hermes reports its available model catalog.
2. The plugin removes router providers and non-text models.
3. The plugin sends Jev the discovered model summaries (up to 128), without credentials.
4. Jev classifies the request into one of five levels.
5. The plugin maps that level to one real model from the current Hermes catalog.
6. Hermes sends the clean request to that model.

The compact route label shows the tier, selected model, and reasoning effort. It marks configured or cross-tier failover with `· fallback`.

## Credentials and privacy

Jev receives the current request and a short recent-conversation summary. Depending on configuration, the decision is sent to TypeSafe or OpenRouter; do not route private data to either service unless your policy allows it.

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
curl -H 'Authorization: Bearer local-router' http://127.0.0.1:8765/health
curl -H 'Authorization: Bearer local-router' http://127.0.0.1:8765/v1/models
```

Demo mode returns a local rehearsal response. It does not call the selected answer model. If `JEV_ROUTER_API_KEY` is set, requests to these endpoints must include it as a bearer token, as shown above.

For live standalone sidecar answers, set `JEV_ROUTER_DEMO=0` **and** configure real route targets with reachable endpoints and any required downstream credentials. Turning demo mode off while routes still use the default `demo://` targets will not make live model calls.

Hermes native mode uses provider credentials already configured in Hermes and does not need this sidecar.

## Standalone sidecar

The repository also contains a small OpenAI-compatible sidecar. It is useful for clients that cannot load a Hermes plugin.

Hermes native mode does not need the sidecar on current Hermes versions. An older Hermes version can use the sidecar as a compatibility bridge.

The example route file is for standalone sidecar completion requests. It is not an OpenClaw route file: the OpenClaw plugin builds routes from OpenClaw's model catalog and sends those choices to the sidecar's decision endpoint.

For standalone live answers, set `JEV_ROUTER_ROUTES_FILE` to a route file containing real endpoints and configure the corresponding credentials. Keep real keys in environment variables; never put them in the route file.

## OpenClaw

OpenClaw has a native plugin in the `openclaw/` directory. It uses OpenClaw's `before_model_resolve` hook. OpenClaw still owns the model call, tools, credentials, and failover.

The OpenClaw plugin prefers text models reported by `openclaw models list --json`. If that command fails or returns no usable models, it falls back to model references in OpenClaw's config; those references are not live-availability checks. OpenRouter answer models are excluded by default and can be enabled with the plugin's `allowOpenRouter` option.

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

Restart the OpenClaw Gateway, then inspect the plugin runtime status:

```bash
openclaw plugins inspect hermes-jev-router --runtime --json
```

The plugin sends the current prompt and safe model facts to the sidecar. Jev selects one of five levels. The plugin returns that level's real `provider/model` pair to OpenClaw. If `showRouteCard` is enabled, its outgoing message is prefixed with a multiline Jev decision card (not the compact native Hermes label).

If Jev returns no valid target, the hook leaves OpenClaw's normal model unchanged. Sidecar or network errors are not explicitly caught in the hook, so handling depends on OpenClaw's hook runner; this plugin does not guarantee the conversation continues unchanged after those errors.

To keep a manually selected model, set `respectManualModel` to `true`. With the default `false`, the plugin routes every normal OpenClaw turn. This is the automatic mode.

## Troubleshooting

### I do not see a route label

Confirm the router provider is active, the `jev-router-inline` plugin is enabled, and `JEV_ROUTER_API_KEY` is set for provider admission. Run the plugin doctor to check hook registration, then test with `hermes -z`. Doctor does not verify a live Jev decision or model call; check the key, network, and provider errors if the routed call fails.

### Jev is unavailable

When Jev cannot make a decision, the router uses the configured `balanced` default. Check `JEV_API_KEY` (or `TYPESAFE_API_KEY`/`OPENROUTER_API_KEY`) and network access if this happens repeatedly.

### A model fails after Jev selects it

The router tries a configured per-tier fallback first, when one exists, then another discovered Hermes target for authentication, quota, and unsupported-model errors. Check the provider's Hermes authentication if this repeats. The compact label adds `· fallback` when a backup target handled the request.

### The available models differ from this README

That is expected. The plugin uses the models available in your Hermes installation. It does not use the author's model list.

## Tests for maintainers

```bash
HERMES_HOME="$(mktemp -d)" python3 -m unittest discover -s tests -v
python3 -m compileall -q hermes_jev_router providers scripts
node --test openclaw/catalog.test.mjs
node --check openclaw/index.js
git diff --check
```

The unit tests cover tier decisions, marker handling and mocked native dispatch/failover, sidecar request paths, and OpenClaw catalog parsing. They do not prove live provider responses, Telegram/Discord delivery, or end-to-end OpenClaw hook behavior.

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
