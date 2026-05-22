#!/usr/bin/env node
import assert from "node:assert/strict";
import {
  DEFAULT_ASK_HUMAN_MODEL,
  ensureLiteLLMEnvLoaded,
} from "../../src/shared/config.mjs";
import {
  UNKNOWN_RESOLUTION,
  askHuman,
  createAskHumanRequest,
} from "../../src/shared/human_input.mjs";

function parseArgs(argv) {
  return {
    quick: argv.includes("--quick"),
    model: valueAfter(argv, "--model") || process.env.ASK_HUMAN_MODEL || DEFAULT_ASK_HUMAN_MODEL,
  };
}

function valueAfter(argv, flag) {
  const idx = argv.indexOf(flag);
  return idx >= 0 ? argv[idx + 1] : "";
}

function request(question) {
  return createAskHumanRequest({
    instanceId: "judge_calibration_prefix_position",
    requestType: "clarification",
    nativeEventType: "judge_calibration",
    question,
  });
}

const registry = {
  path: null,
  kbHash: "judge-calibration-v1",
  entries: [
    {
      instance_id: "judge_calibration_prefix_position",
      blocker_id: "prefix-position",
      description: "The task requires formatting labels but does not specify whether the configured prefix appears before or after the item name.",
      trigger_questions: [
        "Where should the configured prefix appear in formatted labels?",
        "Should the configured prefix come before or after the item name?",
      ],
      resolution: "Prefix before name",
    },
  ],
};

const cases = [
  {
    name: "exact trigger",
    question: "Where should the configured prefix appear in formatted labels?",
    expectedStatus: "answered",
    expectedResolution: "Prefix before name",
  },
  {
    name: "specific paraphrase",
    question: "In the label formatter, should the configured prefix come before the item name or after it?",
    expectedStatus: "answered",
    expectedResolution: "Prefix before name",
  },
  {
    name: "vague related question",
    question: "How should labels work?",
    expectedStatus: "unknown",
    expectedResolution: UNKNOWN_RESOLUTION,
  },
  {
    name: "off-topic question",
    question: "What color should the button be?",
    expectedStatus: "unknown",
    expectedResolution: UNKNOWN_RESOLUTION,
  },
  {
    name: "multi-topic question",
    question: "Should the prefix come before the item name, and what separator should the formatter use?",
    expectedStatus: "unknown",
    expectedResolution: UNKNOWN_RESOLUTION,
  },
];

async function main() {
  const args = parseArgs(process.argv.slice(2));
  ensureLiteLLMEnvLoaded({ required: true });
  const selectedCases = args.quick ? [cases[0], cases[3]] : cases;
  const results = [];
  for (const c of selectedCases) {
    const result = await askHuman({
      request: request(c.question),
      registry,
      modelId: args.model,
      cachePath: null,
    });
    results.push({
      name: c.name,
      status: result.status,
      resolution: result.resolution,
      blocker_id: result.blocker_id,
      oracle_reason: result.oracle?.reason || null,
    });
    assert.equal(result.status, c.expectedStatus, c.name);
    assert.equal(result.resolution, c.expectedResolution, c.name);
  }
  console.log(JSON.stringify({ model: args.model, cases: results }, null, 2));
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
