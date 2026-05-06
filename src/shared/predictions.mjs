import fs from "node:fs/promises";
import path from "node:path";

export function comparePredictions(left, right) {
  const lh = String(left.harness || "");
  const rh = String(right.harness || "");
  if (lh !== rh) return lh.localeCompare(rh);
  const li = String(left.instance_id || "");
  const ri = String(right.instance_id || "");
  if (li !== ri) return li.localeCompare(ri);
  return Number(left.attempt_index || 0) - Number(right.attempt_index || 0);
}

export async function collectRunPredictions(runDir) {
  const files = [];
  async function visit(dir) {
    let entries = [];
    try {
      entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const entryPath = path.join(dir, entry.name);
      if (entry.isDirectory()) await visit(entryPath);
      else if (entry.name === "prediction.json") files.push(entryPath);
    }
  }
  await visit(path.join(runDir, "trajectories"));
  const predictions = [];
  for (const file of files) {
    try {
      predictions.push(JSON.parse(await fs.readFile(file, "utf8")));
    } catch (error) {
      console.error(`Skipping unreadable prediction ${file}: ${error}`);
    }
  }
  predictions.sort(comparePredictions);
  return predictions;
}
