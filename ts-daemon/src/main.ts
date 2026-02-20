#!/usr/bin/env npx tsx
/**
 * CLI entry — parse args, load config, run daemon.
 * Usage: npx tsx src/main.ts --config <path> --agent <name>
 */
import { readFileSync } from "fs";
import { resolve } from "path";
import { loadConfig } from "./config.js";
import { AgentDaemon } from "./daemon.js";

const TOKEN_FILE = resolve(process.env.HOME || "~", ".claude/oauth-token");

function usage(): never {
  console.error("Usage: npx tsx src/main.ts --config <path> --agent <name>");
  process.exit(1);
}

const args = process.argv.slice(2);
let configPath: string | undefined;
let agentName: string | undefined;

for (let i = 0; i < args.length; i++) {
  if (args[i] === "--config" && args[i + 1]) configPath = args[++i];
  else if (args[i] === "--agent" && args[i + 1]) agentName = args[++i];
}

if (!configPath || !agentName) usage();

// Load OAuth token: env var takes precedence, then token file
if (!process.env.CLAUDE_CODE_OAUTH_TOKEN) {
  try {
    const token = readFileSync(TOKEN_FILE, "utf8").trim();
    if (!token) throw new Error("empty");
    process.env.CLAUDE_CODE_OAUTH_TOKEN = token;
    console.log(`Loaded OAuth token from ${TOKEN_FILE}`);
  } catch {
    console.error(`No OAuth token found. Either:`);
    console.error(`  export CLAUDE_CODE_OAUTH_TOKEN=<token>`);
    console.error(`  bash setup-auth.sh`);
    process.exit(1);
  }
}

// Strip nested-session guard at process level
delete process.env.CLAUDECODE;

const config = loadConfig(configPath);
const daemon = new AgentDaemon(config, agentName);
daemon.run().catch((err) => {
  console.error(`daemon fatal: ${err}`);
  process.exit(1);
});
