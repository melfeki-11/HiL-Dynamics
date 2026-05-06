import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { rootDir } from "./config.mjs";
import { appendJsonl, pathExists } from "./io.mjs";
import { redactString } from "./redact.mjs";

export function repoUrl(repo) {
  if (/^https?:\/\//.test(repo)) return repo;
  if (/^file:\/\//.test(repo)) return repo;
  if (path.isAbsolute(repo) || repo.startsWith(".")) return path.resolve(rootDir, repo);
  return `https://github.com/${repo}.git`;
}

async function moveDir(from, to) {
  try {
    await fs.rename(from, to);
  } catch (error) {
    if (error?.code !== "EXDEV") throw error;
    await fs.cp(from, to, { recursive: true });
    await fs.rm(from, { recursive: true, force: true });
  }
}

async function placeWorkspace(from, to, trajectoryFile) {
  if (process.env.HARNESS_WORKSPACE_PLACEMENT === "copy") {
    await moveDir(from, to);
    return;
  }
  await fs.symlink(from, to, "dir");
  await appendJsonl(trajectoryFile, {
    type: "workspace_symlink",
    timestamp: new Date().toISOString(),
    workspace_dir: to,
    target_dir: from,
  });
}

function localTmpDir(label) {
  return path.join(os.tmpdir(), `${label}-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`);
}

export async function runCommand(command, args, options = {}) {
  const { cwd, env, trajectoryFile, allowFailure = false } = options;
  const startedAt = new Date().toISOString();
  if (trajectoryFile) await appendJsonl(trajectoryFile, { type: "command_start", started_at: startedAt, cwd, command, args });
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("close", async (code, signal) => {
      const result = {
        type: "command_end",
        started_at: startedAt,
        ended_at: new Date().toISOString(),
        cwd,
        command,
        args,
        code,
        signal,
        stdout: redactString(stdout),
        stderr: redactString(stderr),
      };
      if (trajectoryFile) await appendJsonl(trajectoryFile, result);
      if (code !== 0 && !allowFailure) reject(new Error(`${command} ${args.join(" ")} exited with ${code}\n${stderr}`));
      else resolve({ code, signal, stdout, stderr });
    });
  });
}

function cacheName(repo) {
  return String(repo).replace(/[^A-Za-z0-9_.-]+/g, "__");
}

async function ensureRepoCache(row, trajectoryFile) {
  const cloneSource = row.clone_repo || row.repo;
  const cacheDir = path.join(rootDir, ".cache", "repos", `${cacheName(cloneSource)}.git`);
  const lockDir = `${cacheDir}.lock`;
  await fs.mkdir(path.dirname(cacheDir), { recursive: true });
  while (true) {
    try {
      await fs.mkdir(lockDir);
      break;
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
  try {
    if (await pathExists(path.join(cacheDir, "HEAD"))) {
      await runCommand("git", ["fetch", "--prune", "origin"], { cwd: cacheDir, trajectoryFile, allowFailure: true });
    } else {
      const tmpDir = localTmpDir(`${cacheName(cloneSource)}-mirror`);
      try {
        await runCommand("git", ["clone", "--mirror", repoUrl(cloneSource), tmpDir], { trajectoryFile });
        await moveDir(tmpDir, cacheDir);
      } catch (error) {
        await fs.rm(tmpDir, { recursive: true, force: true });
        throw error;
      }
    }
  } finally {
    await fs.rm(lockDir, { recursive: true, force: true });
  }
  return cacheDir;
}

export async function cloneCheckout({ row, workspaceDir, trajectoryFile }) {
  const cloneSource = row.clone_repo || row.repo;
  const gitDir = path.join(workspaceDir, ".git");
  await fs.mkdir(path.dirname(workspaceDir), { recursive: true });
  if (!(await pathExists(gitDir))) {
    if (await pathExists(workspaceDir)) {
      const staleDir = `${workspaceDir}.stale-${Date.now()}`;
      await fs.rename(workspaceDir, staleDir);
      await appendJsonl(trajectoryFile, { type: "workspace_quarantine", timestamp: new Date().toISOString(), from: workspaceDir, to: staleDir });
    }
    const tmpDir = localTmpDir(`${cacheName(cloneSource)}-checkout`);
    try {
      let clonedWithCache = false;
      try {
        const cacheDir = await ensureRepoCache(row, trajectoryFile);
        await runCommand("git", ["clone", "--no-tags", "--reference-if-able", cacheDir, repoUrl(cloneSource), tmpDir], { trajectoryFile });
        clonedWithCache = true;
      } catch (error) {
        await appendJsonl(trajectoryFile, { type: "repo_cache_fallback", timestamp: new Date().toISOString(), error: String(error?.stack || error) });
        await fs.rm(tmpDir, { recursive: true, force: true });
      }
      if (!clonedWithCache) {
        await runCommand("git", ["clone", "--no-tags", "--depth", "1", repoUrl(cloneSource), tmpDir], { trajectoryFile });
      }
      await placeWorkspace(tmpDir, workspaceDir, trajectoryFile);
    } catch (error) {
      await fs.rm(tmpDir, { recursive: true, force: true });
      throw error;
    }
  } else {
    await appendJsonl(trajectoryFile, { type: "workspace_reuse", timestamp: new Date().toISOString(), workspace_dir: workspaceDir });
  }
  await runCommand("git", ["fetch", "--depth", "1", "origin", row.base_commit], { cwd: workspaceDir, trajectoryFile });
  await runCommand("git", ["checkout", "-f", row.base_commit], { cwd: workspaceDir, trajectoryFile });
  await runCommand("git", ["reset", "--hard", row.base_commit], { cwd: workspaceDir, trajectoryFile });
  await runCommand("git", ["clean", "-fdx"], { cwd: workspaceDir, trajectoryFile });
}

export async function diff(workspaceDir, trajectoryFile) {
  const result = await runCommand("git", ["diff", "--binary"], { cwd: workspaceDir, trajectoryFile, allowFailure: true });
  return result.stdout;
}

export function attemptWorkspace(attemptDir) {
  return path.join(attemptDir, "repo");
}
