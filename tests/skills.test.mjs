/**
 * Integration checks for ask-human skill install/remove helpers.
 */
import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs/promises";
import os from "node:os";

import {
  SHARED_SKILL_NAME,
  installClaudeSkill,
  removeInstalledAskHumanSkills,
} from "../src/hil_swe/skills.mjs";

test("install then remove clears all harness skill paths", async () => {
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

test("installed skill metadata description stays within Codex limit", async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "hil-swe-skills-metadata-"));
  try {
    await installClaudeSkill(tmp, "AskUserQuestion");
    const skillMd = path.join(tmp, ".claude", "skills", SHARED_SKILL_NAME, "SKILL.md");
    const content = await fs.readFile(skillMd, "utf8");
    const match = content.match(/^---\n([\s\S]*?)\n---/);
    assert.ok(match, "SKILL.md should have frontmatter");

    const descriptionMatch = match[1].match(/^description:\s*\|-\n([\s\S]*)$/m);
    assert.ok(descriptionMatch, "SKILL.md should have a block description");
    const description = descriptionMatch[1]
      .split("\n")
      .map((line) => line.replace(/^ {4}/, ""))
      .join("\n")
      .trim();

    assert.ok(
      description.length <= 1024,
      `skill description is ${description.length} characters`,
    );
  } finally {
    await fs.rm(tmp, { recursive: true, force: true });
  }
});

test("removeInstalledAskHumanSkills is safe on empty workspace", async () => {
  const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "hil-swe-skills-empty-"));
  try {
    await removeInstalledAskHumanSkills(tmp);
  } finally {
    await fs.rm(tmp, { recursive: true, force: true });
  }
});
