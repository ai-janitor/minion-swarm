import { readFileSync } from "fs";
import { resolve } from "path";
import type { AgentConfig } from "./config.js";
import { loadContract } from "./contracts.js";

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
  const contract = loadContract("daemon-rules");
  if (contract) {
    const lines: string[] = ["Autonomous daemon rules:"];
    if (contract.common) {
      for (const rule of contract.common) {
        lines.push(`- ${String(rule).replace(/\{agent\}/g, agent)}`);
      }
    }
    const roleRules = role === "lead" ? contract.lead : contract.non_lead;
    if (roleRules) {
      for (const rule of roleRules) {
        lines.push(`- ${String(rule).replace(/\{agent\}/g, agent)}`);
      }
    }
    return lines.join("\n");
  }

  // Fallback: hardcoded rules
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
  const contract = loadContract("compaction-markers");
  if (contract?.history_block) {
    const tmpl = contract.history_block;
    const header = tmpl.header || "════════════════════ RECENT HISTORY (rolling buffer) ════════════════════";
    const preamble = tmpl.preamble || "The following is your captured history from before compaction.\nUse it to restore recent context and avoid redoing completed work.";
    const footer = tmpl.footer || "═══════════════════════ END RECENT HISTORY ═════════════════════════════";
    return [header, preamble, snapshot, footer].join("\n");
  }

  // Fallback
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
  const contract = loadContract("boot-sequence");

  let boot: string;
  if (contract) {
    // Use first 3 commands from contract (NO check-inbox)
    const commands: string[] = (contract.commands || [])
      .slice(0, 3)
      .map((cmd: string) =>
        cmd.replace(/\{agent\}/g, agent).replace(/\{role\}/g, cfg.role)
      );
    const commandBlock = commands.map((c: string) => `  ${c}`).join("\n");
    const preamble = String(contract.preamble || "BOOT: You just started. Run these commands via the Bash tool:")
      .replace(/\{agent\}/g, agent).replace(/\{role\}/g, cfg.role);
    const postamble = String(contract.postamble || "")
      .replace(/\{agent\}/g, agent).replace(/\{role\}/g, cfg.role);
    boot = [preamble, commandBlock, "", postamble].join("\n");
  } else {
    // Fallback: hardcoded boot
    boot = [
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
  }

  return [
    cfg.system.trim(),
    protocolSection(agent, cfg.role),
    rulesSection(agent, cfg.role),
    boot,
  ].join("\n\n");
}

/** Subsequent invocations — messages/tasks already inline from poll. */
export function buildInboxPrompt(agent: string, cfg: AgentConfig, pollData: Record<string, any>, historySnapshot?: string): string {
  const system = stripOnStartup(cfg.system.trim());
  const sections: string[] = [system, protocolSection(agent, cfg.role)];

  if (historySnapshot) {
    sections.push(historyBlock(historySnapshot));
  }

  const contract = loadContract("inbox-template");

  const inboxLines: string[] = [];
  const messages = (pollData.messages || []) as any[];
  if (messages.length > 0) {
    const inboxHeader = contract?.inbox_header
      ? contract.inbox_header.replace(/\{agent\}/g, agent)
      : "=== INBOX (already consumed — do NOT run check-inbox) ===";
    inboxLines.push(inboxHeader);

    const msgFmt = contract?.message_format || "FROM {sender}: {content}";
    for (const msg of messages) {
      const sender = msg.from_agent || "unknown";
      const content = msg.content || "(empty)";
      inboxLines.push(
        msgFmt
          .replace(/\{sender\}/g, sender)
          .replace(/\{content\}/g, content)
          .replace(/\{agent\}/g, agent)
      );
    }

    const inboxFooter = contract?.inbox_footer || "=== END INBOX ===";
    inboxLines.push(inboxFooter);
  }

  const tasks = (pollData.tasks || []) as any[];
  if (tasks.length > 0) {
    inboxLines.push(contract?.task_header || "=== AVAILABLE TASKS ===");
    const taskFmt = contract?.task_format || "- [{task_id}] {title} ({status}) — {claim_cmd}";
    for (const task of tasks) {
      inboxLines.push(
        taskFmt
          .replace(/\{task_id\}/g, task.task_id || "")
          .replace(/\{title\}/g, task.title || "")
          .replace(/\{status\}/g, task.status || "")
          .replace(/\{claim_cmd\}/g, task.claim_cmd || "")
          .replace(/\{agent\}/g, agent)
      );
    }
    inboxLines.push(contract?.task_footer || "=== END TASKS ===");
  }

  if (inboxLines.length > 0) {
    sections.push(inboxLines.join("\n"));
  }

  const postInstructions = contract?.post_instructions
    ? contract.post_instructions
        .map((line: string) => line.replace(/\{agent\}/g, agent))
        .join("\n")
    : [
        "Process the messages/tasks above, then send results:",
        `  minion send --from ${agent} --to <recipient> --message '...'`,
        "Do NOT re-register — you are already registered.",
        "Do NOT run check-inbox — messages are already shown above.",
      ].join("\n");

  sections.push(rulesSection(agent, cfg.role), postInstructions);
  return sections.filter((s) => s.trim()).join("\n\n");
}
