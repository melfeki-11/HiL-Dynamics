/**
 * HiL-SWE ask limits: optional fixed per-pass cap, irrelevant cooldown,
 * irrelevant-first throttle, and read-before-ask guard (all env-gated).
 */

import { UNKNOWN_RESOLUTION } from "../shared/human_input.mjs";

const CAP_RESPONSE =
  "Question budget exhausted for this attempt. Proceed with implementation using your best inference; do not call ask_human again until the next attempt.";

const COOLDOWN_RESPONSE =
  "Two prior questions were judged irrelevant. Do not ask another question until you have read at least 2 new files. If the answer is still unclear, make your best implementation choice and document it.";

const READ_BEFORE_ASK_RESPONSE =
  "Read at least 2 relevant repository files (e.g. README and the module under change) before asking. Use Read/Grep first, then ask one identifier-anchored question.";

export function parseMaxAsksPerPass() {
  const v = Number(process.env.MAX_ASKS_PER_PASS ?? "");
  return Number.isFinite(v) && v > 0 ? Math.floor(v) : 0;
}

export function parseIrrelevantCooldown() {
  const v = Number(process.env.IRRELEVANT_COOLDOWN ?? "");
  return Number.isFinite(v) && v > 0 ? Math.floor(v) : 0;
}

export function isIrrelevantFirstThrottleEnabled() {
  return /^(1|true|yes|on)$/i.test(String(process.env.IRRELEVANT_FIRST_THROTTLE || ""));
}

export function isReadBeforeAskEnabled() {
  return /^(1|true|yes|on)$/i.test(String(process.env.READ_BEFORE_ASK || ""));
}

export function readBeforeAskMinFiles() {
  const v = Number(process.env.READ_BEFORE_ASK_MIN_FILES ?? "2");
  return Number.isFinite(v) && v > 0 ? Math.floor(v) : 2;
}

export function resolveMaxAsksPerPass() {
  return parseMaxAsksPerPass();
}

export function createAskLimitTracker(opts = {}) {
  const maxAsksPerPass = opts.maxAsksPerPass ?? resolveMaxAsksPerPass();
  const irrelevantCooldownThreshold = opts.irrelevantCooldownThreshold ?? parseIrrelevantCooldown();
  const irrelevantFirstThrottle = opts.irrelevantFirstThrottle ?? isIrrelevantFirstThrottleEnabled();
  const irrelevantFirstMin = Number(opts.irrelevantFirstMin ?? 1);
  const readBeforeAsk = opts.readBeforeAsk ?? isReadBeforeAskEnabled();
  const readBeforeAskMin = opts.readBeforeAskMin ?? readBeforeAskMinFiles();

  const anyGuard =
    maxAsksPerPass > 0 ||
    irrelevantCooldownThreshold > 0 ||
    readBeforeAsk;

  if (!anyGuard && !irrelevantFirstThrottle) {
    return {
      checkBeforeJudge() {
        return { shortCircuit: false };
      },
      notifyRoutedToJudge() {},
      recordJudgeResolution() {},
      recordBlockerResolved() {},
      noteFileRead() {},
      clearReadHistory() {},
      capResponse: CAP_RESPONSE,
      cooldownResponse: COOLDOWN_RESPONSE,
      readBeforeAskResponse: READ_BEFORE_ASK_RESPONSE,
    };
  }

  let routedAsksThisPass = 0;
  let consecutiveIrrelevant = 0;
  const filesReadSinceLastAsk = new Set();

  return {
    capResponse: CAP_RESPONSE,
    cooldownResponse: COOLDOWN_RESPONSE,
    readBeforeAskResponse: READ_BEFORE_ASK_RESPONSE,

    noteFileRead(filePath) {
      const p = String(filePath || "").trim();
      if (p) filesReadSinceLastAsk.add(p);
    },

    clearReadHistory() {
      filesReadSinceLastAsk.clear();
    },

    recordBlockerResolved() {},

    checkBeforeJudge() {
      if (readBeforeAsk && filesReadSinceLastAsk.size < readBeforeAskMin) {
        return {
          shortCircuit: true,
          reason: "read_before_ask",
          responseText: READ_BEFORE_ASK_RESPONSE,
        };
      }
      if (irrelevantCooldownThreshold > 0 && consecutiveIrrelevant >= irrelevantCooldownThreshold) {
        consecutiveIrrelevant = 0;
        return { shortCircuit: true, reason: "cooldown", responseText: COOLDOWN_RESPONSE };
      }

      const capActive =
        maxAsksPerPass > 0 &&
        (!irrelevantFirstThrottle || consecutiveIrrelevant >= irrelevantFirstMin);

      if (capActive && routedAsksThisPass >= maxAsksPerPass) {
        return { shortCircuit: true, reason: "cap", responseText: CAP_RESPONSE };
      }

      return { shortCircuit: false };
    },

    notifyRoutedToJudge() {
      routedAsksThisPass += 1;
      filesReadSinceLastAsk.clear();
    },

    recordJudgeResolution(resolution, { blockerId, status } = {}) {
      const r = String(resolution || "");
      if (r.trim() === UNKNOWN_RESOLUTION.trim()) {
        consecutiveIrrelevant += 1;
      } else {
        consecutiveIrrelevant = 0;
      }
      const bid = String(blockerId || "");
      void bid;
      void status;
    },
  };
}
