import path from "node:path";
import fs from "node:fs/promises";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const SHARED_SKILL_NAME = "clarify-information";

export const SKILL_TOOL_REF = {
  claude: "AskUserQuestion",
  codex: "requestUserInput",
  adk: "ask_human",
  opencode: "ask_human",
};

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SHARED_SKILL_TEMPLATE_PATH = path.join(__dirname, "templates", "ask_human_skill.md");
const SHARED_SKILL_TEMPLATE = readFileSync(SHARED_SKILL_TEMPLATE_PATH, "utf8");

function renderSharedSkill(toolName) {
  return SHARED_SKILL_TEMPLATE.replaceAll("{{TOOL_NAME}}", String(toolName || ""));
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
