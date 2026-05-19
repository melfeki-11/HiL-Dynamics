/**
 * Integration checks for ask-human skill install/remove helpers.
 */
import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs/promises";
import { readdirSync } from "node:fs";
import os from "node:os";

const templatesDir = path.resolve("src/hil_swe/templates");
const skillTemplates = readdirSync(templatesDir)
  .filter((name) => name.endsWith(".md"))
  .sort();
if (!skillTemplates.length) {
  throw new Error(`No skill templates found in ${templatesDir}`);
}
process.env.WITH_SKILL = process.env.WITH_SKILL || skillTemplates[0].slice(0, -3);

let skillsModulePromise = null;
async function loadSkillsModule() {
  if (!skillsModulePromise) {
    skillsModulePromise = import("../src/hil_swe/skills.mjs");
  }
  return skillsModulePromise;
}

test("install then remove clears all harness skill paths", async () => {
  const { SHARED_SKILL_NAME, installClaudeSkill, removeInstalledAskHumanSkills } = await loadSkillsModule();
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "hil-swe-skills-"));
  try {
    await installClaudeSkill(tmp, "AskUserQuestion");
    const skillMd = path.join(tmp, ".claude", "skills", SHARED_SKILL_NAME, "SKILL.md");
    await fs.access(skillMd);

    await removeInstalledAskHumanSkills(tmp);
    await assert.rejects(() => fs.access(skillMd));
  } finally {
    await fs.rm(tmp, { recursive: true, force: true });
  }
});

test("removeInstalledAskHumanSkills is safe on empty workspace", async () => {
  const { removeInstalledAskHumanSkills } = await loadSkillsModule();
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "hil-swe-skills-empty-"));
  try {
    await removeInstalledAskHumanSkills(tmp);
  } finally {
    await fs.rm(tmp, { recursive: true, force: true });
  }
});
