import fs from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const inProcessLeases = new Map();
let nextPort = portStart();

function portStart() {
  return Number(process.env.OPENCODE_PORT_START || 4096);
}

function portEnd() {
  return Number(process.env.OPENCODE_PORT_END || 4196);
}

function lockRoot() {
  return process.env.OPENCODE_PORT_LOCK_DIR || path.join(os.tmpdir(), "trust-horizon-opencode-ports");
}

function staleLeaseMs() {
  return Number(process.env.OPENCODE_PORT_LEASE_STALE_MS || 6 * 60 * 60 * 1000);
}

function lockPathForPort(port) {
  return path.join(lockRoot(), `${port}.lock`);
}

export function opencodePortRange() {
  const start = portStart();
  const end = portEnd();
  return {
    start,
    end,
    size: Math.max(0, end - start + 1),
    lock_dir: lockRoot(),
  };
}

async function isPortFree(port) {
  if (process.env.OPENCODE_PORT_SKIP_BIND_CHECK === "1") return true;
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => {
      server.close(() => resolve(true));
    });
    server.listen(port, "127.0.0.1");
  });
}

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

async function maybeRemoveStaleLease(lockPath) {
  const staleMs = staleLeaseMs();
  if (!Number.isFinite(staleMs) || staleMs <= 0) return false;
  try {
    const text = await fs.readFile(lockPath, "utf8");
    const stat = await fs.stat(lockPath);
    const parsed = JSON.parse(text);
    const oldEnough = Date.now() - stat.mtimeMs > staleMs;
    if (oldEnough && !pidAlive(Number(parsed.pid))) {
      await fs.unlink(lockPath);
      return true;
    }
  } catch (error) {
    if (error?.code === "ENOENT") return false;
    if (error instanceof SyntaxError) {
      const stat = await fs.stat(lockPath).catch(() => null);
      if (stat && Date.now() - stat.mtimeMs > staleMs) {
        await fs.unlink(lockPath).catch(() => {});
        return true;
      }
    }
  }
  return false;
}

async function tryLeasePort(port, owner) {
  if (inProcessLeases.has(port)) return false;
  await fs.mkdir(lockRoot(), { recursive: true });
  const leasePath = lockPathForPort(port);
  let handle = null;
  try {
    handle = await fs.open(leasePath, "wx");
    await handle.writeFile(
      JSON.stringify({
        pid: process.pid,
        host: os.hostname(),
        port,
        owner: owner || null,
        created_at: new Date().toISOString(),
      }) + "\n",
      "utf8"
    );
    if (!(await isPortFree(port))) {
      await handle.close().catch(() => {});
      await fs.unlink(leasePath).catch(() => {});
      return false;
    }
    inProcessLeases.set(port, leasePath);
    return true;
  } catch (error) {
    if (handle) await handle.close().catch(() => {});
    if (error?.code === "EEXIST") {
      if (await maybeRemoveStaleLease(leasePath)) return tryLeasePort(port, owner);
      return false;
    }
    throw error;
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

export async function availableOpencodePorts() {
  const { start, end } = opencodePortRange();
  let available = 0;
  for (let port = start; port <= end; port += 1) {
    await maybeRemoveStaleLease(lockPathForPort(port));
    if (!inProcessLeases.has(port) && (await isPortFree(port))) {
      try {
        await fs.access(lockPathForPort(port));
      } catch (error) {
        if (error?.code === "ENOENT") available += 1;
        else throw error;
      }
    }
  }
  return available;
}

export async function acquirePort({ owner } = {}) {
  const { start, end } = opencodePortRange();
  if (start > end) throw new Error(`Invalid OpenCode port range ${start}-${end}`);
  if (nextPort < start || nextPort > end) nextPort = start;
  const initialPort = nextPort;
  while (true) {
    const port = nextPort;
    nextPort = nextPort >= end ? start : nextPort + 1;
    if (await tryLeasePort(port, owner)) return port;
    if (nextPort === initialPort) {
      throw new Error(`No free OpenCode server ports in ${start}-${end}; lock dir ${lockRoot()}`);
    }
  }
}

export async function releasePort(port) {
  const leasePath = inProcessLeases.get(port);
  inProcessLeases.delete(port);
  if (!leasePath) return;
  await fs.unlink(leasePath).catch((error) => {
    if (error?.code !== "ENOENT") throw error;
  });
}
