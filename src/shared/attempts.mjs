import fs from "node:fs/promises";
import path from "node:path";
import { ensureDir, pathExists } from "./io.mjs";

export async function archiveExistingAttempt({ runDir, attemptDir, harnessName, instanceId, attemptIndex }) {
  if (!(await pathExists(attemptDir))) return null;
  const archiveRoot = path.join(runDir, "stale-attempts", harnessName, instanceId);
  await ensureDir(archiveRoot);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  let target = path.join(archiveRoot, `attempt-${attemptIndex}-${stamp}`);
  let suffix = 1;
  while (await pathExists(target)) {
    target = path.join(archiveRoot, `attempt-${attemptIndex}-${stamp}-${suffix}`);
    suffix += 1;
  }
  await fs.rename(attemptDir, target);
  return target;
}
