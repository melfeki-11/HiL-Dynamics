import path from "node:path";
import fs from "node:fs/promises";

export const SHARED_SKILL_NAME = "clarify-information";
export const SHARED_SKILL_DESCRIPTION =
  "Get clarification for an implementation detail that is unclear and whose resolution cannot be found in the problem statement or codebase. Use when there is a missing parameter, unclear value, ambiguous requirement or preference, contradiction between two instructions or specifications, and other such information that might have a chance of blocking you from implementing the task successfully.";

export const SKILL_TOOL_REF = {
  claude: "AskUserQuestion",
  codex: "requestUserInput",
  adk: "ask_human",
  opencode: "ask_human",
};

function renderSharedSkill(toolName) {
  return `---
name: ${SHARED_SKILL_NAME}
description: ${SHARED_SKILL_DESCRIPTION}
---

## When to use this skill
Use this skill when you need to clarify an implementation detail that is unclear or confusing and you cannot find a definitive resolution to it in the provided problem statement or codebase. Examples include:
- A missing parameter
- An unclear value
- An ambiguous requirement or preference
- A contradiction between two instructions or specifications
- Other such missing or confusing information that can block you from implementing the task successfully

Do NOT use this skill to clarify implementation details that are purely cosmetic or completely irrelevant to the task at hand.

## How to use this skill
1. Identify what is the missing piece of information, ambiguous information, or contradictory information that you need to clarify.
2. Use the \`${toolName}\` tool to send one single well-formed question, targeted exactly to the detail you need to clarify, to a human expert.

Follow the rules below PRECISELY when using \`${toolName}\`:
- Submit only ONE, clear, specific question at a time, targeting one specific detail.
- Your detail to be clarified should be specific and clear.
- If the expert deems your question irrelevant, but you believe it's a necessary clarification, try asking again but reword, structure, or format your question differently. An expert response of "irrelevant question" doesn't just come from asking a useless question; it could also be because you did not ask a specific-enough question, or because you put more than one question in one tool call.
- If the expert answers your question, **do not ask about the same detail again.** Always immediately incorporate their clarification into your problem-solving process.
- Always integrate previous expert answers into your problem solving process to unblock you in your implementation or so you can ask follow-up questions.

## Avoid the following mistakes when using this skill
1. NEVER submit multiple questions in one tool call. **If there are multiple details you want to clarify, you MUST use this skill multiple times, asking questions one by one.**
2. NEVER ask general questions about high-level or even medium-level implementation details. E.g. "How should I implement function X?" is a bad question that will NOT be answered. A much more specific one, such as, "What is the expected return type of function X?" CAN be answered.
`;
}

async function installSkillAt(baseDir, toolName) {
  const skillDir = path.join(baseDir, SHARED_SKILL_NAME);
  await fs.mkdir(skillDir, { recursive: true });
  await fs.writeFile(path.join(skillDir, "SKILL.md"), renderSharedSkill(toolName), "utf8");
  return skillDir;
}

export async function installAgentsSkill(workspaceDir, toolName) {
  return installSkillAt(path.join(workspaceDir, ".agents", "skills"), toolName);
}

export async function installClaudeSkill(workspaceDir, toolName) {
  return installSkillAt(path.join(workspaceDir, ".claude", "skills"), toolName);
}
