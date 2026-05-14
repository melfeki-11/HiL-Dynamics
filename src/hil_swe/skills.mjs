import path from "node:path";
import fs from "node:fs/promises";

export const SHARED_SKILL_NAME = "clarify-information";
export const SHARED_SKILL_DESCRIPTION =
  "Use for coding tasks with missing, ambiguous, or contradictory information. Use when you've identified such information and it would be helpful to get more clarification from the human expert before doing the next implementation step.";

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

Understand what is the missing piece of information, ambiguity, or contradiction present in the task you need to clarify. Then, use the \`${toolName}\` tool to get clarification from the human expert.

Rules for using the \`${toolName}\` tool:
- Submit only ONE, clear, specific question at a time, targeting one specific detail. Never ask multiple questions in one tool call.
- Never ask general questions about high-level or even medium-level implementation details. E.g. "How should I implement function X?" is a bad question that will NOT be answered by the expert. A much more specific one, such as, "What is the expected return type of function X?" CAN be answered by the expert.
- If the expert deems your question irrelevant, but you believe it's a necessary clarification, try asking again but word, structure, or format your question differently. An irrelevant classification doesn't just come from asking a useless question; it could also be because you did not ask a specific-enough question, or because you put more than one question in one tool call.
- If the expert answers your question, **do not ask about the same detail again.** Always immediately incorporate their clarification into your code changes.
- Always integrate previous expert answers into your problem solving process to unblock you in your implementation or so you can ask follow-up questions.
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

export async function installOpenCodeSkill(workspaceDir, toolName) {
  return installSkillAt(path.join(workspaceDir, ".opencode", "skills"), toolName);
}

export async function installClaudeSkill(workspaceDir, toolName) {
  return installSkillAt(path.join(workspaceDir, ".claude", "skills"), toolName);
}
