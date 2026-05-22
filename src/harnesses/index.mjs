import { harness as claudeCode } from "./claude-code/index.mjs";
import { harness as codex } from "./codex/index.mjs";
import { harness as opencode } from "./opencode/index.mjs";

export const harnesses = new Map([
  [claudeCode.name, claudeCode],
  [codex.name, codex],
  [opencode.name, opencode],
]);

export function resolveHarnesses(name) {
  if (name === "all") return [...harnesses.values()];
  const harness = harnesses.get(name);
  if (!harness) throw new Error(`Unknown harness ${name}. Expected one of: all, ${[...harnesses.keys()].join(", ")}`);
  return [harness];
}
