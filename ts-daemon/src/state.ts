import { readFileSync, writeFileSync, mkdirSync } from "fs";
import { dirname } from "path";
import { execFileSync } from "child_process";

export interface DaemonState {
  agent: string;
  pid: number;
  status: string;
  updatedAt: string;
  consecutiveFailures: number;
  resumeReady: boolean;
  [key: string]: unknown;
}

export function writeState(statePath: string, agent: string, status: string, extra: Record<string, unknown> = {}): void {
  mkdirSync(dirname(statePath), { recursive: true });
  const state: DaemonState = {
    agent,
    pid: process.pid,
    status,
    updatedAt: new Date().toISOString(),
    consecutiveFailures: 0,
    resumeReady: false,
    ...extra,
  };
  // Write snake_case keys to disk
  const { agent: _a, pid, status: _s, updatedAt, consecutiveFailures, resumeReady, ...rest } = state;
  const payload: Record<string, unknown> = {
    agent: state.agent,
    pid: state.pid,
    status: state.status,
    updated_at: updatedAt,
    consecutive_failures: consecutiveFailures,
    resume_ready: resumeReady,
    ...rest,
  };
  writeFileSync(statePath, JSON.stringify(payload, null, 2));
}

export function loadResumeReady(statePath: string): boolean {
  try {
    const data = JSON.parse(readFileSync(statePath, "utf8"));
    return Boolean(data?.resume_ready ?? data?.resumeReady);
  } catch {
    return false;
  }
}

/** Call minion update-hp to write observed HP to SQLite. */
export function updateHp(
  agent: string,
  inputTokens: number,
  outputTokens: number,
  limit: number,
  turnInput?: number,
  turnOutput?: number,
): void {
  const args = [
    "update-hp",
    "--agent", agent,
    "--input-tokens", String(inputTokens),
    "--output-tokens", String(outputTokens),
    "--limit", String(limit),
  ];
  if (turnInput !== undefined) args.push("--turn-input", String(turnInput));
  if (turnOutput !== undefined) args.push("--turn-output", String(turnOutput));

  // Strip CLAUDECODE so nested sessions don't refuse to start
  const env = { ...process.env };
  delete env.CLAUDECODE;
  env.MINION_CLASS = "lead";

  try {
    execFileSync("minion", args, { env, timeout: 10_000, stdio: "ignore" });
  } catch (err) {
    log(agent, `update-hp failed: ${err}`);
  }
}

export function log(agent: string, message: string): void {
  const ts = new Date().toISOString().replace("T", " ").slice(0, 19);
  console.log(`[${ts}] [${agent}] ${message}`);
}
