/**
 * Unit tests for HiL-SWE ask limit tracker.
 * Run: node --test tests/ask_limits.test.mjs
 */
import test from "node:test";
import assert from "node:assert/strict";
import { UNKNOWN_RESOLUTION } from "../src/shared/human_input.mjs";
import {
  createAskLimitTracker,
  resolveMaxAsksPerPass,
} from "../src/hil_swe/ask_limits.mjs";

test("resolveMaxAsksPerPass: defaults to unbounded unless fixed cap is set", () => {
  const prevMax = process.env.MAX_ASKS_PER_PASS;
  delete process.env.MAX_ASKS_PER_PASS;
  assert.equal(resolveMaxAsksPerPass(3), 0);
  process.env.MAX_ASKS_PER_PASS = "5";
  assert.equal(resolveMaxAsksPerPass(99), 5);
  if (prevMax) process.env.MAX_ASKS_PER_PASS = prevMax;
  else delete process.env.MAX_ASKS_PER_PASS;
});

test("cap short-circuits after K routed judge calls", () => {
  const t = createAskLimitTracker({
    maxAsksPerPass: 2,
    irrelevantCooldownThreshold: 0,
    irrelevantFirstThrottle: false,
  });
  assert.equal(t.checkBeforeJudge().shortCircuit, false);
  t.notifyRoutedToJudge();
  t.recordJudgeResolution("answered");
  assert.equal(t.checkBeforeJudge().shortCircuit, false);
  t.notifyRoutedToJudge();
  t.recordJudgeResolution("answered again");
  const g = t.checkBeforeJudge();
  assert.equal(g.shortCircuit, true);
  assert.equal(g.reason, "cap");
});

test("irrelevant-first: cap not active until after first irrelevant", () => {
  const t = createAskLimitTracker({
    maxAsksPerPass: 2,
    irrelevantFirstThrottle: true,
    irrelevantFirstMin: 1,
  });
  t.notifyRoutedToJudge();
  t.recordJudgeResolution("answered");
  t.notifyRoutedToJudge();
  t.recordJudgeResolution("answered");
  // Still no irrelevant — cap should not block third ask yet
  assert.equal(t.checkBeforeJudge().shortCircuit, false);
  t.notifyRoutedToJudge();
  t.recordJudgeResolution(UNKNOWN_RESOLUTION);
  const g = t.checkBeforeJudge();
  assert.equal(g.shortCircuit, true);
  assert.equal(g.reason, "cap");
});

test("cooldown short-circuits after N consecutive irrelevant resolutions", () => {
  const t = createAskLimitTracker({
    maxAsksPerPass: 0,
    irrelevantCooldownThreshold: 2,
    irrelevantFirstThrottle: false,
  });
  assert.equal(t.checkBeforeJudge().shortCircuit, false);
  t.notifyRoutedToJudge();
  t.recordJudgeResolution(UNKNOWN_RESOLUTION);
  assert.equal(t.checkBeforeJudge().shortCircuit, false);
  t.notifyRoutedToJudge();
  t.recordJudgeResolution(UNKNOWN_RESOLUTION);
  const g = t.checkBeforeJudge();
  assert.equal(g.shortCircuit, true);
  assert.equal(g.reason, "cooldown");
});

test("read-before-ask blocks until enough files noted", () => {
  const t = createAskLimitTracker({
    readBeforeAsk: true,
    readBeforeAskMin: 2,
    maxAsksPerPass: 0,
  });
  const g0 = t.checkBeforeJudge();
  assert.equal(g0.shortCircuit, true);
  assert.equal(g0.reason, "read_before_ask");
  t.noteFileRead("README.md");
  assert.equal(t.checkBeforeJudge().shortCircuit, true);
  t.noteFileRead("src/foo.py");
  assert.equal(t.checkBeforeJudge().shortCircuit, false);
});
