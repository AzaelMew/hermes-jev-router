import test from "node:test";
import assert from "node:assert/strict";
import { buildFivePointRoutes, parseOpenClawModelRows, modelsFromConfig } from "./catalog.mjs";

test("uses only available text models and excludes OpenRouter by default", () => {
  const models = parseOpenClawModelRows({ models: [
    { key: "openai/gpt-5.6-luna", name: "Luna", available: true },
    { key: "openai/gpt-5.4-mini", name: "Mini", available: true },
    { key: "openai/gpt-5.4-nano", name: "Nano", available: false },
    { key: "openrouter/expensive", name: "Router", available: true },
    { key: "fal/image-model", name: "Image", input: "image", available: true },
  ]});
  assert.deepEqual(models.map((item) => item.model), ["gpt-5.6-luna", "gpt-5.4-mini"]);
});

test("spreads the local inventory across five route slots", () => {
  const models = parseOpenClawModelRows({ models: Array.from({ length: 9 }, (_, index) => ({
    key: `provider/model-${index}`,
    name: `Model ${index}`,
    available: true,
  }))});
  const routes = buildFivePointRoutes(models);
  assert.deepEqual(Object.keys(routes), ["micro", "cheap", "balanced", "strong", "frontier"]);
  assert.equal(new Set(Object.values(routes).map((item) => `${item.provider}/${item.model}`)).size, 5);
});

test("falls back to configured OpenClaw refs without inventing models", () => {
  const models = modelsFromConfig({
    agents: { defaults: { model: { primary: "openai/primary", fallbacks: ["xai/fallback"] }, models: { "openai/alias": {} } } },
    models: { providers: { custom: { models: [{ id: "local" }] } } },
  });
  assert.deepEqual(models.map((item) => `${item.provider}/${item.model}`), [
    "openai/primary", "xai/fallback", "openai/alias", "custom/local",
  ]);
});

// Created by Codex GPT-6 on 2026-09-18 12:00 PDT on ombee.
