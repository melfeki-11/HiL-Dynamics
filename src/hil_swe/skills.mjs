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
const TEMPLATES_DIR = path.join(__dirname, "templates");
const TEMPLATE_VERSION_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const SKILL_TEMPLATE_VERSION = String(process.env.WITH_SKILL || "").trim();

function renderSharedSkill(toolName) {
  if (!SKILL_TEMPLATE_VERSION) {
    throw new Error("WITH_SKILL must be set to a template version when skill installation is enabled.");
  }
  if (!TEMPLATE_VERSION_RE.test(SKILL_TEMPLATE_VERSION)) {
    throw new Error(
      `Invalid WITH_SKILL=${JSON.stringify(SKILL_TEMPLATE_VERSION)}. ` +
      "Use only letters, digits, dot, underscore, or hyphen.",
    );
  }
  const fileName = `${SKILL_TEMPLATE_VERSION}.md`;
  const templatePath = path.join(TEMPLATES_DIR, fileName);
  if (!path.isAbsolute(templatePath)) {
    throw new Error(`Resolved skill template path is invalid: ${templatePath}`);
  }
  let template;
  try {
    template = readFileSync(templatePath, "utf8");
  } catch {
    throw new Error(
      `WITH_SKILL=${JSON.stringify(SKILL_TEMPLATE_VERSION)} requires ${fileName} in ${TEMPLATES_DIR}.`,
    );
  }
  void toolName;
  return template;
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

/**
 * Remove the shared ask-human SKILL.md tree from every harness location we might
 * have written to, so full_info runs do not leave a discoverable skill on disk.
 */
export async function removeInstalledAskHumanSkills(workspaceDir) {
  const roots = [
    path.join(workspaceDir, ".claude", "skills", SHARED_SKILL_NAME),
    path.join(workspaceDir, ".agents", "skills", SHARED_SKILL_NAME),
    path.join(workspaceDir, ".opencode", "skills", SHARED_SKILL_NAME),
    path.join(workspaceDir, "skills", SHARED_SKILL_NAME),
  ];
  for (const p of roots) {
    try {
      await fs.rm(p, { recursive: true, force: true });
    } catch {
      // ignore
    }
  }
}
