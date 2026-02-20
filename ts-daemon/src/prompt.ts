import { readFileSync } from "fs";
import { resolve } from "path";
import type { AgentConfig } from "./config.js";

const PROTOCOL_DIR = resolve(
  process.env.MINION_DOCS_DIR || resolve(process.env.HOME || "~", ".minion_work", "docs")
);

/** Strip ON STARTUP block from system prompt for subsequent invocations. */
function stripOnStartup(text: string): string {
  return text
    .replace(
      /ON STARTUP[^\n]*\n(?:[ \t]+\d+\..*\n)*(?:[ \t]+Then .*\n?)?/g,
      "",
    )
    .trim();
}

/** Read protocol-common.md + protocol-{role}.md from ~/.minion-comms/docs/ */
function loadProtocolDocs(role: string): string {
  const files = [
    resolve(PROTOCOL_DIR, "protocol-common.md"),
    resolve(PROTOCOL_DIR, `protocol-${role}.md`),
  ];
  const sections: string[] = [];
  for (const f of files) {
    try {
      sections.push(readFileSync(f, "utf8").trim());
    } catch {
      // Protocol doc not found — skip silently
    }
  }
  return sections.join("\n\n");
}

function protocolSection(agent: string, role: string): string {
  const docs = loadProtocolDocs(role);
  if (docs) return docs;
  // Fallback if protocol docs not installed
  return [
    "Communication protocol — use the `minion` CLI via Bash tool:",
    `- Check inbox: minion check-inbox --agent ${agent}`,
    `- Send message: minion send --from ${agent} --to <recipient> --message '...'`,
    `- Set status: minion set-status --agent ${agent} --status '...'`,
    `- Set context: minion set-context --agent ${agent} --context '...'`,
    `- View agents: minion who`,
    "- All minion commands output JSON. Use Bash tool to run them.",
  ].join("\n");
}

function rulesSection(agent: string, role: string): string {
  const lines = [
    "Autonomous daemon rules:",
    "- Do not use AskUserQuestion — it blocks in headless mode.",
    `- Route questions to lead via Bash: minion send --from ${agent} --to lead --message '...'`,
    "- Execute exactly the incoming task.",
    "- Send one summary message when done.",
    "- Task governance: lead manages task queue and assignment ownership.",
  ];

  if (role === "lead") {
    lines.push(
      "- As lead: create and maintain tasks.",
      "- As lead: define scope and acceptance criteria.",
      "- As lead: ask domain owners to update technical details based on direct work.",
      "- As lead: after a task completes, review and assign the next task.",
    );
  } else {
    lines.push(
      "- Non-lead agents: execute assigned tasks, report results.",
      "- If you discover new ideas, send them to lead.",
    );
  }

  return lines.join("\n");
}

function historyBlock(snapshot: string): string {
  return [
    "════════════════════ RECENT HISTORY (rolling buffer) ════════════════════",
    "The following is your captured history from before compaction.",
    "Use it to restore recent context and avoid redoing completed work.",
    "══════════════════════════════════════════════════════════════════════════",
    snapshot,
    "═══════════════════════ END RECENT HISTORY ═════════════════════════════",
  ].join("\n");
}

/** First invocation — agent registers and sets up. */
export function buildBootPrompt(agent: string, cfg: AgentConfig): string {
  const boot = [
    "BOOT: You just started. Run these commands via the Bash tool:",
    `  minion --compact register --name ${agent} --class ${cfg.role} --transport daemon`,
    `  minion set-context --agent ${agent} --context 'just started'`,
    `  minion check-inbox --agent ${agent}`,
    `  minion set-status --agent ${agent} --status 'ready for orders'`,
    "",
    "IMPORTANT: You are a daemon agent managed by minion-swarm.",
    "Do NOT run poll.sh — minion-swarm handles polling for you.",
    "Do NOT use AskUserQuestion — it blocks in headless mode.",
  ].join("\n");

  return [
    cfg.system.trim(),
    protocolSection(agent, cfg.role),
    rulesSection(agent, cfg.role),
    boot,
  ].join("\n\n");
}

/** Subsequent invocations — check inbox and process messages. */
export function buildInboxPrompt(agent: string, cfg: AgentConfig, historySnapshot?: string): string {
  const system = stripOnStartup(cfg.system.trim());
  const sections: string[] = [system, protocolSection(agent, cfg.role)];

  if (historySnapshot) {
    sections.push(historyBlock(historySnapshot));
  }

  const inbox = [
    "You have new messages. Run via Bash tool:",
    `  minion check-inbox --agent ${agent}`,
    "Read and process all messages, then send results:",
    `  minion send --from ${agent} --to <recipient> --message '...'`,
    "Do NOT re-register — you are already registered.",
  ].join("\n");

  sections.push(rulesSection(agent, cfg.role), inbox);
  return sections.filter((s) => s.trim()).join("\n\n");
}
