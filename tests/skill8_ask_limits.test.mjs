/**
 * Unit tests for skill8/9 ask limit tracker.
 * Run: node --test tests/skill8_ask_limits.test.mjs
 */
import test from "node:test";
import assert from "node:assert/strict";
import { UNKNOWN_RESOLUTION } from "../src/shared/human_input.mjs";
import {
  createSkill8AskLimitTracker,
  resolveMaxAsksPerPass,
} from "../src/hil_swe/skill8_ask_limits.mjs";

test("resolveMaxAsksPerPass: blocker-scaled min(6, n+1)", () => {
  const prev = process.env.BLOCKER_SCALED_CAP;
  const prevMax = process.env.MAX_ASKS_PER_PASS;
  process.env.BLOCKER_SCALED_CAP = "1";
  delete process.env.MAX_ASKS_PER_PASS;
  assert.equal(resolveMaxAsksPerPass(3), 4);
  assert.equal(resolveMaxAsksPerPass(9), 6);
  process.env.BLOCKER_SCALED_CAP = prev || "";
  if (prevMax) process.env.MAX_ASKS_PER_PASS = prevMax;
});

test("cap short-circuits after K routed judge calls", () => {
  const t = createSkill8AskLimitTracker({
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
  const t = createSkill8AskLimitTracker({
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
  const t = createSkill8AskLimitTracker({
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
  const t = createSkill8AskLimitTracker({
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

test("registry stop after blockers resolved", () => {
  const t = createSkill8AskLimitTracker({
    maxAsksPerPass: 0,
    registryStop: true,
    numBlockersTotal: 3,
  });
  t.recordJudgeResolution("ok", { blockerId: "b1", status: "answered" });
  t.recordJudgeResolution("ok", { blockerId: "b2", status: "answered" });
  t.recordJudgeResolution("ok", { blockerId: "b3", status: "answered" });
  const g = t.checkBeforeJudge();
  assert.equal(g.shortCircuit, true);
  assert.equal(g.reason, "registry_stop");
});
