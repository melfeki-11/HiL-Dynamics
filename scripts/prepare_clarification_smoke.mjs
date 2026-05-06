#!/usr/bin/env node
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";
import { dataDir, rootDir } from "../src/shared/config.mjs";

const execFileAsync = promisify(execFile);

const fixtureRoot = path.join(rootDir, ".cache", "clarification-smoke");
const repoDir = path.join(fixtureRoot, "repo");
const instanceId = "smoke_prefix_format";

async function run(command, args, options = {}) {
  await execFileAsync(command, args, { ...options, maxBuffer: 10 * 1024 * 1024 });
}

async function write(filePath, text) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, text, "utf8");
}

async function main() {
  await fs.rm(repoDir, { recursive: true, force: true });
  await fs.mkdir(repoDir, { recursive: true });
  await write(
    path.join(repoDir, "labeler.py"),
    `def format_label(name, prefix=None):
    """Return a display label for a product name."""
    if not prefix:
        return name
    return f"{name} {prefix}"
`
  );
  await write(
    path.join(repoDir, "README.md"),
    `# Clarification Smoke Fixture

This tiny repository is used only to validate harness clarification plumbing.
`
  );
  await run("git", ["init"], { cwd: repoDir });
  await run("git", ["config", "user.email", "harness@example.invalid"], { cwd: repoDir });
  await run("git", ["config", "user.name", "Harness Smoke"], { cwd: repoDir });
  await run("git", ["add", "labeler.py", "README.md"], { cwd: repoDir });
  await run("git", ["commit", "-m", "initial smoke fixture"], { cwd: repoDir });
  const { stdout } = await execFileAsync("git", ["rev-parse", "HEAD"], { cwd: repoDir });
  const baseCommit = stdout.trim();

  const row = {
    repo: "local/clarification-smoke",
    clone_repo: repoDir,
    base_commit: baseCommit,
    instance_id: instanceId,
    problem_statement:
      "Implement configured prefix support in format_label. When a prefix is configured, the display label should include it. Keep the no-prefix behavior unchanged.",
    hints_text:
      "The public issue intentionally omits the product convention for prefix placement and separator. The agent should ask a clarification instead of guessing.",
    created_at: "2026-05-01T00:00:00Z",
    version: "clarification-smoke-v1",
    fail_to_pass: ["test_labeler.py::test_prefix_format", "test_labeler.py::test_empty_prefix"],
    pass_to_pass: [],
    test_patch: `diff --git a/test_labeler.py b/test_labeler.py
new file mode 100644
--- /dev/null
+++ b/test_labeler.py
@@ -0,0 +1,9 @@
+from labeler import format_label
+
+
+def test_prefix_format():
+    assert format_label("Widget", "SKU-7") == "SKU-7: Widget"
+
+
+def test_empty_prefix():
+    assert format_label("Widget", "") == "Widget"
`,
    patch: `diff --git a/labeler.py b/labeler.py
--- a/labeler.py
+++ b/labeler.py
@@ -2,4 +2,4 @@ def format_label(name, prefix=None):
     """Return a display label for a product name."""
     if not prefix:
         return name
-    return f"{name} {prefix}"
+    return f"{prefix}: {name}"
`,
  };

  const kb = {
    version: 1,
    description: "Registry-only human answers for the synthetic clarification smoke fixture.",
    entries: [
      {
        id: "b_001",
        instance_id: instanceId,
        request_type: "clarification",
        blocker_id: "prefix-format-convention",
        type: "missing_information",
        description: "The product convention for configured label prefix placement and separator is omitted from the public task.",
        selector:
          "The agent asks about the required label prefix convention, including placement, separator, or no-prefix behavior.",
        trigger_questions: [
          "Where should the configured prefix appear in formatted labels?",
          "What separator should be used between prefix and item name?",
          "What should format_label return when prefix is empty?",
        ],
        selected_labels: ["Prefix before name, separated by a colon-space"],
        resolution: "Use the prefix before the item name followed by ': '; if prefix is empty, return the name unchanged.",
        resolution_source: "human",
        action_critical: true,
        observable_after: null,
        commit_boundary: null,
      },
    ],
  };

  await fs.mkdir(dataDir, { recursive: true });
  await write(path.join(dataDir, "clarification_smoke.jsonl"), `${JSON.stringify(row)}\n`);
  await write(path.join(dataDir, "clarification_smoke_kb.json"), `${JSON.stringify(kb, null, 2)}\n`);
  console.log(`Wrote ${path.join(dataDir, "clarification_smoke.jsonl")}`);
  console.log(`Wrote ${path.join(dataDir, "clarification_smoke_kb.json")}`);
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
