import { execFile as execFileCallback } from "node:child_process";
import { promisify } from "node:util";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import {
  buildFivePointRoutes,
  modelsFromConfig,
  parseOpenClawModelRows,
  toJevModels,
} from "./catalog.mjs";

const execFile = promisify(execFileCallback);
const CARD_START = "╭─ ⚡ Jev model routing ─╮";
const DEFAULT_ENDPOINT = "http://127.0.0.1:8765/v1/jev/decision";

function asBoolean(value, fallback) {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    if (["1", "true", "yes", "on"].includes(value.toLowerCase())) return true;
    if (["0", "false", "no", "off"].includes(value.toLowerCase())) return false;
  }
  return fallback;
}

function pluginOptions(value) {
  const config = value && typeof value === "object" ? value : {};
  return {
    enabled: asBoolean(config.enabled, true),
    showRouteCard: asBoolean(config.showRouteCard, true),
    respectManualModel: asBoolean(config.respectManualModel, false),
    allowOpenRouter: asBoolean(config.allowOpenRouter, false),
    toolsPresent: asBoolean(config.toolsPresent, true),
    endpoint: String(config.endpoint || DEFAULT_ENDPOINT).trim() || DEFAULT_ENDPOINT,
    catalogTtlSeconds: Math.max(5, Math.min(3600, Number(config.catalogTtlSeconds) || 60)),
    requestTimeoutMs: Math.max(1000, Math.min(15000, Number(config.requestTimeoutMs) || 6500)),
    openclawBin: String(config.openclawBin || process.env.OPENCLAW_BIN || "openclaw").trim(),
    routerApiKeyEnv: String(config.routerApiKeyEnv || "JEV_ROUTER_API_KEY").trim(),
  };
}

function configuredDefault(config) {
  return String(config?.agents?.defaults?.model?.primary || "").trim();
}

async function liveCatalog(options, api, agentId) {
  const args = ["models", "list", "--json"];
  if (agentId) args.push("--agent", agentId);
  try {
    const result = await execFile(options.openclawBin, args, {
      timeout: options.requestTimeoutMs,
      maxBuffer: 2 * 1024 * 1024,
      windowsHide: true,
    });
    const parsed = JSON.parse(result.stdout);
    const models = parseOpenClawModelRows(parsed, { allowOpenRouter: options.allowOpenRouter });
    if (models.length) return models;
    api.logger.warn("[jev-router] OpenClaw returned no usable text models");
  } catch (error) {
    api.logger.warn(`[jev-router] OpenClaw model discovery failed: ${String(error).split("\n")[0].slice(0, 180)}`);
  }
  return modelsFromConfig(api.config, { allowOpenRouter: options.allowOpenRouter });
}

async function postDecision(options, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.requestTimeoutMs);
  try {
    const headers = { "content-type": "application/json", accept: "application/json" };
    const routerKey = options.routerApiKeyEnv ? process.env[options.routerApiKeyEnv] : "";
    if (routerKey) headers.authorization = `Bearer ${routerKey}`;
    const response = await fetch(options.endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`router HTTP ${response.status}`);
    const parsed = await response.json();
    if (!parsed || typeof parsed !== "object" || !parsed.decision) throw new Error("router returned no decision");
    return parsed;
  } finally {
    clearTimeout(timer);
  }
}

function selectedTarget(result, routes) {
  const target = result?.decision?.target;
  if (!target || typeof target !== "object") return null;
  const provider = String(target.provider || "").trim().toLowerCase();
  const model = String(target.model || "").trim();
  if (!provider || !model) return null;
  const allowed = Object.values(routes).some((item) => item.provider === provider && item.model === model);
  return allowed ? { provider, model } : null;
}

function routeKey(ctx) {
  return ctx?.sessionKey || ctx?.runId || null;
}

export default definePluginEntry({
  id: "hermes-jev-router",
  name: "Hermes Jev Router",
  description: "Use TypeSafe Jev to select the least expensive capable OpenClaw model.",
  register(api) {
    const options = pluginOptions(api.pluginConfig);
    if (!options.enabled) {
      api.logger.info("[jev-router] disabled by plugin config");
      return;
    }

    const catalogCache = new Map();
    const pendingRoutes = new Map();

    async function getCatalog(agentId) {
      const key = agentId || "main";
      const cached = catalogCache.get(key);
      if (cached && Date.now() - cached.loadedAt < options.catalogTtlSeconds * 1000) return cached;
      const models = await liveCatalog(options, api, agentId);
      const snapshot = { loadedAt: Date.now(), models, routes: buildFivePointRoutes(models) };
      if (snapshot.models.length) catalogCache.set(key, snapshot);
      return snapshot;
    }

    api.on("before_model_resolve", async (event, ctx) => {
      if (options.respectManualModel) {
        const current = ctx?.modelProviderId && ctx?.modelId ? `${ctx.modelProviderId}/${ctx.modelId}` : "";
        const primary = configuredDefault(api.config);
        if (current && primary && current !== primary) return;
      }
      const snapshot = await getCatalog(ctx?.agentId);
      if (!snapshot.models.length || Object.keys(snapshot.routes).length === 0) {
        api.logger.warn("[jev-router] no OpenClaw model inventory; kept the normal model");
        return;
      }
      const result = await postDecision(options, {
        prompt: String(event?.prompt || ""),
        platform: ctx?.channel || "openclaw",
        model: ctx?.modelProviderId && ctx?.modelId ? `${ctx.modelProviderId}/${ctx.modelId}` : "auto",
        tools_present: options.toolsPresent,
        available_models: toJevModels(snapshot.models),
        route_choices: snapshot.routes,
      });
      const target = selectedTarget(result, snapshot.routes);
      if (!target) {
        api.logger.warn("[jev-router] Jev returned no valid OpenClaw target; kept the normal model");
        return;
      }
      const key = routeKey(ctx);
      if (key) pendingRoutes.set(key, { card: result.card, target, selectedAt: Date.now() });
      api.logger.info(`[jev-router] ${target.provider}/${target.model} selected from ${snapshot.models.length} available models`);
      return { providerOverride: target.provider, modelOverride: target.model };
    }, { priority: 100, timeoutMs: 15000 });

    api.on("message_sending", (event, ctx) => {
      if (!options.showRouteCard || !event?.content || event.content.includes(CARD_START)) return;
      const key = routeKey(ctx);
      const pending = key ? pendingRoutes.get(key) : null;
      if (!pending) return;
      pendingRoutes.delete(key);
      return { content: `${pending.card}\n\n${event.content}` };
    });

    api.logger.info("[jev-router] enabled; OpenClaw catalog discovery and Jev routing are active");
  },
});

// Created by Codex GPT-6 on 2026-09-18 12:00 PDT on ombee.
