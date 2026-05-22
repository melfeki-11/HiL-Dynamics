import test from "node:test";
import assert from "node:assert/strict";

const ACT_CAP = 4000;
const UNKNOWN_RESOLUTION = "irrelevant question";

function cap(s, limit) {
  const str = String(s || "");
  return str.length > limit ? `${str.slice(0, limit)}… [truncated]` : str;
}

function isAskUserQuestionTool(toolName) {
  return /AskUserQuestion|askUserQuestion/.test(String(toolName || ""));
}

function isCustomAskHumanTool(toolName) {
  return String(toolName || "") === "mcp__human_input__ask_human";
}

function formatAct(toolName, toolInput) {
  const name = String(toolName || "");
  if (!name) return "";
  let q = "";
  if (typeof toolInput?.question === "string") q = toolInput.question;
  else if (typeof toolInput?.questions?.[0]?.question === "string") q = toolInput.questions[0].question;
  else if (typeof toolInput === "string") q = toolInput;
  else {
    try { q = JSON.stringify(toolInput || {}); } catch { q = ""; }
  }
  if (isCustomAskHumanTool(name)) return `ask_human [custom_tool] ${q}`;
  if (isAskUserQuestionTool(name)) return `ask_human [native] ${q}`;
  if (/ask_human/i.test(name)) return `ask_human [other] ${q}`;
  return `${name}: ${JSON.stringify(toolInput || {})}`;
}

function extractAskQuestionFullInfoStep(event) {
  const pairs = Array.isArray(event.qa_pairs) && event.qa_pairs.length
    ? event.qa_pairs
    : [{ question: event.question, answer: UNKNOWN_RESOLUTION }];
  return pairs.map((pair) => ({
    thought: "",
    act: cap(`ask_human [native] ${pair?.question || ""}`, ACT_CAP),
    obs: String(pair?.answer ?? UNKNOWN_RESOLUTION),
  }));
}

test("formatAct marks Claude native ask tool with [native]", () => {
  const act = formatAct("AskUserQuestion", { question: "What version should this target?" });
  assert.equal(act, "ask_human [native] What version should this target?");
});

test("formatAct marks Claude custom ask tool with [custom_tool]", () => {
  const act = formatAct("mcp__human_input__ask_human", { question: "What is the expected output?" });
  assert.equal(act, "ask_human [custom_tool] What is the expected output?");
});

test("full_info ask_question event is serialized with native prefix", () => {
  const [step] = extractAskQuestionFullInfoStep({
    question: "Do we need backward compatibility?",
    qa_pairs: [{ question: "Do we need backward compatibility?", answer: UNKNOWN_RESOLUTION }],
  });
  assert.equal(step.act, "ask_human [native] Do we need backward compatibility?");
  assert.equal(step.obs, UNKNOWN_RESOLUTION);
});
