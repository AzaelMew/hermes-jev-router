/**
 * Safe OpenClaw model discovery and five-point route spreading.
 *
 * OpenClaw owns the live model list.  This file only keeps non-secret model
 * facts that Jev needs for classification.
 */

export const TIERS = ["micro", "cheap", "balanced", "strong", "frontier"];

const ROUTER_PROVIDERS = new Set(["jev-router", "jev", "auto-router", "openrouter"]);

function number(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function positiveInteger(value) {
  const parsed = Math.floor(number(value));
  return parsed > 0 ? parsed : 0;
}

function splitModelKey(value) {
  const key = String(value || "").trim();
  const separator = key.indexOf("/");
  if (separator <= 0 || separator === key.length - 1) return null;
  return { provider: key.slice(0, separator).toLowerCase(), model: key.slice(separator + 1) };
}

function isReasoningModel(text) {
  return /(?:reason|thinking|opus|pro|max|ultra|codex|o[1-9])/.test(text);
}

function qualityScore(item) {
  const text = `${item.provider}/${item.model} ${item.name || ""}`.toLowerCase();
  let score = 1;
  if (item.reasoning) score += 1;
  if (item.context_window >= 200_000) score += 1;
  if (item.context_window >= 1_000_000) score += 0.5;
  for (const [token, points] of [
    ["nano", -1.2], ["mini", -0.9], ["flash", -0.5], ["haiku", -0.5],
    ["small", -0.6], ["lite", -0.8], ["fast", -0.3],
    ["pro", 1], ["max", 1], ["opus", 1.2], ["ultra", 1],
    ["sol", 1], ["terra", 0.6], ["astra", 1.1],
  ]) {
    if (text.includes(token)) score += points;
  }
  return score;
}

function costScore(item) {
  const explicit = number(item.cost_input, NaN) + number(item.cost_output, NaN);
  if (Number.isFinite(explicit)) return explicit;
  const text = `${item.provider}/${item.model} ${item.name || ""}`.toLowerCase();
  let score = qualityScore(item);
  for (const [token, points] of [
    ["nano", -3], ["mini", -2], ["flash", -1.5], ["haiku", -1.5],
    ["small", -1.5], ["lite", -2], ["fast", -0.8],
    ["pro", 2], ["max", 2], ["opus", 3], ["ultra", 2],
  ]) {
    if (text.includes(token)) score += points;
  }
  return score;
}

function normalizedModel(item) {
  return {
    provider: item.provider,
    model: item.model,
    name: item.name || `${item.provider}/${item.model}`,
    reasoning: Boolean(item.reasoning),
    input_modalities: item.input_modalities || ["text"],
    context_window: positiveInteger(item.context_window),
    supports_tools: item.supports_tools !== false,
    supports_streaming: item.supports_streaming !== false,
    ...(Number.isFinite(Number(item.cost_input)) ? { cost_input: number(item.cost_input) } : {}),
    ...(Number.isFinite(Number(item.cost_output)) ? { cost_output: number(item.cost_output) } : {}),
  };
}

/** Convert `openclaw models list --json` output into safe model facts. */
export function parseOpenClawModelRows(payload, { allowOpenRouter = false } = {}) {
  const rows = Array.isArray(payload) ? payload : payload?.models;
  if (!Array.isArray(rows)) return [];
  const result = [];
  const seen = new Set();
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    if (row.available === false || row.missing === true) continue;
    const key = splitModelKey(row.key || row.id || row.model);
    if (!key || ROUTER_PROVIDERS.has(key.provider) && !(key.provider === "openrouter" && allowOpenRouter)) continue;
    const input = String(row.input || "").toLowerCase();
    if (input && input !== "-" && !input.includes("text")) continue;
    const identity = `${key.provider}:${key.model}`;
    if (seen.has(identity)) continue;
    seen.add(identity);
    const name = String(row.name || key.model);
    const contextWindow = positiveInteger(row.contextWindow ?? row.context_window);
    result.push(normalizedModel({
      provider: key.provider,
      model: key.model,
      name,
      reasoning: row.reasoning ?? isReasoningModel(`${key.model} ${name}`.toLowerCase()),
      input_modalities: input && input !== "-" ? input.split("+") : ["text"],
      context_window: contextWindow,
      supports_tools: row.supportsTools ?? row.supports_tools ?? true,
      supports_streaming: row.supportsStreaming ?? row.supports_streaming ?? true,
      cost_input: row.cost?.input ?? row.cost_input,
      cost_output: row.cost?.output ?? row.cost_output,
    }));
  }
  return result;
}

/** Fallback for hosts where the CLI catalog is temporarily unavailable. */
export function modelsFromConfig(config, { allowOpenRouter = false } = {}) {
  const refs = new Set();
  const defaults = config?.agents?.defaults || {};
  const primary = defaults.model?.primary;
  if (primary) refs.add(primary);
  for (const ref of defaults.model?.fallbacks || []) refs.add(ref);
  for (const ref of Object.keys(defaults.models || {})) refs.add(ref);
  for (const [provider, entry] of Object.entries(config?.models?.providers || {})) {
    for (const model of entry?.models || []) {
      const id = typeof model === "string" ? model : model?.id;
      if (id) refs.add(`${provider}/${id}`);
    }
  }
  return parseOpenClawModelRows(
    [...refs].map((key) => ({ key, available: true })),
    { allowOpenRouter },
  );
}

function pickAtFraction(source, fraction, used) {
  if (!source.length) return null;
  const start = Math.round(fraction * (source.length - 1));
  for (let offset = 0; offset < source.length; offset += 1) {
    const candidate = source[(start + offset) % source.length];
    const identity = `${candidate.provider}:${candidate.model}`;
    if (!used.has(identity)) {
      used.add(identity);
      return candidate;
    }
  }
  return source[start];
}

/** Spread the local OpenClaw inventory across five real model targets. */
export function buildFivePointRoutes(models) {
  if (!models.length) return {};
  const eligible = models.filter((item) => item.supports_tools);
  const source = eligible.length ? eligible : models;
  const byCost = [...source].sort((a, b) => costScore(a) - costScore(b) || a.model.localeCompare(b.model));
  const byQuality = [...source].sort((a, b) => qualityScore(a) - qualityScore(b) || a.model.localeCompare(b.model));
  const used = new Set();
  const picks = [
    pickAtFraction(byCost, 0, used),
    pickAtFraction(byCost, 0.25, used),
    pickAtFraction(byQuality, 0.5, used),
    pickAtFraction(byQuality, 0.75, used),
    pickAtFraction(byQuality, 1, used),
  ].filter(Boolean);
  while (picks.length < TIERS.length) picks.push(picks[picks.length - 1] || source[0]);
  return Object.fromEntries(TIERS.map((tier, index) => {
    const item = picks[index];
    return [tier, {
      provider: item.provider,
      model: item.model,
      supports_tools: item.supports_tools,
      supports_streaming: item.supports_streaming,
    }];
  }));
}

export function toJevModels(models) {
  return models.map((item) => ({
    provider: item.provider,
    model: item.model,
    name: item.name,
    reasoning: item.reasoning,
    input_modalities: item.input_modalities,
    context_window: item.context_window,
    supports_tools: item.supports_tools,
    supports_streaming: item.supports_streaming,
    ...(item.cost_input !== undefined ? { cost_input: item.cost_input } : {}),
    ...(item.cost_output !== undefined ? { cost_output: item.cost_output } : {}),
  }));
}

// Created by Codex GPT-6 on 2026-09-18 12:00 PDT on ombee.
