import { readFileSync } from "fs";
import { resolve, dirname } from "path";
import yaml from "js-yaml";
import { loadContract } from "./contracts.js";

const configDefaults = loadContract("config-defaults");

export interface AgentConfig {
  name: string;
  role: string;
  zone: string;
  system: string;
  allowedTools?: string[];
  permissionMode?: "default" | "acceptEdits" | "bypassPermissions" | "plan";
  model?: string;
  maxHistoryTokens: number;
  maxPromptChars: number;
  noOutputTimeoutSec: number;
  retryBackoffSec: number;
  retryBackoffMaxSec: number;
}

export interface SwarmConfig {
  configPath: string;
  projectDir: string;
  commsDb: string;
  agents: Record<string, AgentConfig>;
  runtimeDir: string;
  logsDir: string;
  stateDir: string;
}

function resolvePath(raw: string, base: string): string {
  const expanded = raw.replace(/^~/, process.env.HOME || "~");
  if (expanded.startsWith("/")) return expanded;
  return resolve(base, expanded);
}

export function loadConfig(configPath: string): SwarmConfig {
  const cfgPath = resolve(configPath.replace(/^~/, process.env.HOME || "~"));
  const raw = yaml.load(readFileSync(cfgPath, "utf8")) as Record<string, any>;
  if (!raw || typeof raw !== "object") throw new Error("Config must be a YAML mapping");

  const cfgDir = dirname(cfgPath);
  const projectDir = resolvePath(String(raw.project_dir || cfgDir), cfgDir);

  const dbDefault = process.env.MINION_COMMS_DB_PATH
    || process.env.MINION_SWARM_COMMS_DB
    || "~/.minion_work/minion.db";
  const commsDb = resolvePath(String(raw.comms_db || dbDefault), cfgDir);

  const agentsRaw = raw.agents;
  if (!agentsRaw || typeof agentsRaw !== "object") {
    throw new Error("Config must define a non-empty 'agents' mapping");
  }

  const agents: Record<string, AgentConfig> = {};

  for (const [name, item] of Object.entries(agentsRaw as Record<string, any>)) {
    if (!item || typeof item !== "object") {
      throw new Error(`Agent '${name}' config must be a mapping`);
    }

    const role = String(item.role || "coder");
    const zone = String(item.zone || "");
    let system = String(item.system || "").trim();
    if (!system) {
      system = `You are ${name} (${role}) running under minion-swarm. Check inbox, execute tasks, and report when done.`;
    }

    // Parse allowed_tools: "Bash Read Glob" → ["Bash", "Read", "Glob"]
    let allowedTools: string[] | undefined;
    if (item.allowed_tools) {
      allowedTools = String(item.allowed_tools).replace(/,/g, " ").split(/\s+/).filter(Boolean);
    }

    const permissionMode = item.permission_mode
      ? (String(item.permission_mode).trim() as AgentConfig["permissionMode"])
      : undefined;

    agents[name] = {
      name,
      role,
      zone,
      system,
      allowedTools,
      permissionMode,
      model: item.model ? String(item.model) : undefined,
      maxHistoryTokens: Number(item.max_history_tokens ?? configDefaults?.max_history_tokens ?? 100_000),
      maxPromptChars: Number(item.max_prompt_chars ?? configDefaults?.max_prompt_chars ?? 120_000),
      noOutputTimeoutSec: Number(item.no_output_timeout_sec ?? configDefaults?.no_output_timeout_sec ?? 600),
      retryBackoffSec: Number(item.retry_backoff_sec ?? configDefaults?.retry_backoff_sec ?? 30),
      retryBackoffMaxSec: Number(item.retry_backoff_max_sec ?? configDefaults?.retry_backoff_max_sec ?? 300),
    };
  }

  const runtimeDir = resolve(projectDir, ".minion-swarm");
  return {
    configPath: cfgPath,
    projectDir,
    commsDb,
    agents,
    runtimeDir,
    logsDir: resolve(runtimeDir, "logs"),
    stateDir: resolve(runtimeDir, "state"),
  };
}
