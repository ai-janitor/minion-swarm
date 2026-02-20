import { execFileSync } from "child_process";
import { resolve } from "path";
import { mkdirSync } from "fs";
import { query, type SDKMessage, type SDKResultMessage } from "@anthropic-ai/claude-code";
import type { SwarmConfig, AgentConfig } from "./config.js";
import { RollingBuffer } from "./rolling-buffer.js";
import { writeState, updateHp, log } from "./state.js";
import { buildBootPrompt, buildInboxPrompt } from "./prompt.js";

const MINION_CLI = "minion";
const CLAUDE_EXECUTABLE = resolve(process.env.HOME || "~", ".local/bin/claude");
const MAX_CONSOLE_CHARS = 12_000;

interface InvokeResult {
  sessionId: string;
  inputTokens: number;
  outputTokens: number;
  contextWindow: number;
  compactionDetected: boolean;
  isError: boolean;
}

export class AgentDaemon {
  private readonly config: SwarmConfig;
  private readonly agent: AgentConfig;
  private readonly agentName: string;
  private readonly buffer: RollingBuffer;
  private readonly statePath: string;

  private stopRequested = false;
  private injectHistoryNextTurn = false;
  private consecutiveFailures = 0;
  private lastError?: string;
  private invocation = 0;
  private sessionInputTokens = 0;
  private sessionOutputTokens = 0;
  private contextWindow = 0;

  constructor(config: SwarmConfig, agentName: string) {
    if (!config.agents[agentName]) {
      throw new Error(`Unknown agent '${agentName}' in config`);
    }
    this.config = config;
    this.agent = config.agents[agentName];
    this.agentName = agentName;
    this.buffer = new RollingBuffer(this.agent.maxHistoryTokens);
    this.statePath = resolve(config.stateDir, `${agentName}.json`);
  }

  async run(): Promise<void> {
    mkdirSync(this.config.logsDir, { recursive: true });
    mkdirSync(this.config.stateDir, { recursive: true });

    process.on("SIGTERM", () => { this.stopRequested = true; });
    process.on("SIGINT", () => { this.stopRequested = true; });

    log(this.agentName, `starting daemon for ${this.agentName}`);
    log(this.agentName, `mode: SDK (poll: ${MINION_CLI} poll)`);
    this.writeState("idle");

    // Reset stale HP from previous session
    updateHp(this.agentName, 0, 0, 200_000, 0, 0);

    // Boot: invoke agent for ON STARTUP
    log(this.agentName, "boot: invoking agent for ON STARTUP");
    this.writeState("working");
    const bootResult = await this.invoke(buildBootPrompt(this.agentName, this.agent));

    if (!bootResult.isError) {
      this.trackTokens(bootResult);
      log(this.agentName, `boot: complete (${bootResult.inputTokens} in / ${bootResult.outputTokens} out)`);
    } else {
      log(this.agentName, "boot: failed");
    }

    this.writeState("idle");

    // Main poll loop
    while (!this.stopRequested) {
      log(this.agentName, "polling for messages...");
      const hasMessages = this.pollInbox();

      if (this.stopRequested) break;
      if (!hasMessages) continue;

      // Messages available — invoke agent
      this.writeState("working");
      log(this.agentName, "messages detected, invoking agent");

      const historySnapshot = this.injectHistoryNextTurn && this.buffer.length > 0
        ? this.buffer.snapshot()
        : undefined;
      if (historySnapshot) this.injectHistoryNextTurn = false;

      const prompt = buildInboxPrompt(this.agentName, this.agent, historySnapshot);
      const result = await this.invoke(prompt);

      if (result.compactionDetected) {
        this.injectHistoryNextTurn = true;
        log(this.agentName, "detected context compaction; history will be re-injected next cycle");
      }

      if (!result.isError) {
        this.trackTokens(result);
        this.consecutiveFailures = 0;
        this.lastError = undefined;
        this.writeState("idle");
      } else {
        this.consecutiveFailures++;
        this.lastError = "agent invocation failed";
        this.writeState("error", {
          failures: this.consecutiveFailures,
          lastError: this.lastError,
        });
        const backoff = Math.min(
          this.agent.retryBackoffSec * 2 ** (this.consecutiveFailures - 1),
          this.agent.retryBackoffMaxSec,
        );
        log(this.agentName, `failure #${this.consecutiveFailures}; backing off ${backoff}s`);
        await this.sleep(backoff * 1000);
      }
    }

    this.writeState("stopped");
    log(this.agentName, "daemon stopped");
  }

  /** Run `minion poll` as a subprocess. Returns true if messages found. */
  private pollInbox(): boolean {
    try {
      execFileSync(MINION_CLI, ["poll", "--agent", this.agentName, "--interval", "5", "--timeout", "30"], {
        encoding: "utf8",
        timeout: 60_000,
        stdio: ["ignore", "pipe", "pipe"],
      });
      return true; // exit 0 = messages or tasks waiting
    } catch (err: any) {
      const exitCode = err.status ?? 1;
      if (exitCode === 3) {
        log(this.agentName, "stand_down detected — leader dismissed the party");
        this.stopRequested = true;
        return false;
      }
      if (exitCode === 1) return false; // timeout, no messages
      log(this.agentName, `minion poll error (exit ${exitCode})`);
      return false;
    }
  }

  /** Invoke Claude Code SDK — replaces subprocess + stream-json parsing. */
  private async invoke(prompt: string): Promise<InvokeResult> {
    this.invocation++;
    const ts = new Date().toISOString().slice(11, 19);
    console.log(`\n=== model-stream start: agent=${this.agentName} v=${this.invocation} ts=${ts} ===`);

    let displayedChars = 0;
    let hiddenChars = 0;
    let compactionDetected = false;
    let result: InvokeResult = {
      sessionId: "",
      inputTokens: 0,
      outputTokens: 0,
      contextWindow: 0,
      compactionDetected: false,
      isError: false,
    };

    try {
      const stream = query({
        prompt,
        options: {
          allowedTools: this.agent.allowedTools,
          permissionMode: this.agent.permissionMode || "bypassPermissions",
          model: this.agent.model,
          cwd: this.config.projectDir,
          maxTurns: 50,
          env: {
            ...process.env,
            MINION_CLASS: this.agent.role,
            // Strip nested-session guard so spawned claude doesn't refuse to start
            CLAUDECODE: "",
          },
          pathToClaudeCodeExecutable: CLAUDE_EXECUTABLE,
        },
      });

      for await (const msg of stream) {
        // Buffer raw message for compaction recovery
        this.buffer.append(JSON.stringify(msg) + "\n");

        switch (msg.type) {
          case "system":
            if (msg.subtype === "compact_boundary") {
              compactionDetected = true;
            }
            break;

          case "assistant":
            for (const block of msg.message.content) {
              if (block.type === "text" && block.text) {
                const remaining = MAX_CONSOLE_CHARS - displayedChars;
                if (remaining > 0) {
                  const chunk = block.text.slice(0, remaining);
                  process.stdout.write(chunk);
                  displayedChars += chunk.length;
                }
                hiddenChars += Math.max(0, block.text.length - Math.max(remaining, 0));
              }
            }
            break;

          case "result":
            result = this.extractResult(msg, compactionDetected);
            break;
        }
      }
    } catch (err) {
      log(this.agentName, `invoke error: ${err}`);
      result.isError = true;
    }

    if (hiddenChars > 0) {
      console.log(`\n[model-stream abbreviated: ${hiddenChars} chars hidden]`);
    }
    const endTs = new Date().toISOString().slice(11, 19);
    console.log(`=== model-stream end: agent=${this.agentName} v=${this.invocation} ts=${endTs} shown=${displayedChars} chars ===`);

    return result;
  }

  private extractResult(msg: SDKResultMessage, compactionDetected: boolean): InvokeResult {
    let inputTokens = 0;
    let outputTokens = 0;
    let contextWindow = 0;

    // modelUsage has per-model breakdowns with cache-inclusive totals
    for (const usage of Object.values(msg.modelUsage)) {
      inputTokens += usage.inputTokens + usage.cacheReadInputTokens + usage.cacheCreationInputTokens;
      outputTokens += usage.outputTokens;
      if (usage.contextWindow > 0) contextWindow = usage.contextWindow;
    }

    return {
      sessionId: msg.session_id,
      inputTokens,
      outputTokens,
      contextWindow,
      compactionDetected,
      isError: msg.is_error,
    };
  }

  private trackTokens(result: InvokeResult): void {
    this.sessionInputTokens += result.inputTokens;
    this.sessionOutputTokens += result.outputTokens;
    if (result.contextWindow > 0) this.contextWindow = result.contextWindow;

    const limit = this.contextWindow > 0 ? this.contextWindow : 200_000;
    updateHp(
      this.agentName,
      this.sessionInputTokens,
      this.sessionOutputTokens,
      limit,
      result.inputTokens,
      result.outputTokens,
    );
  }

  private writeState(status: string, extra: Record<string, unknown> = {}): void {
    writeState(this.statePath, this.agentName, status, {
      consecutiveFailures: this.consecutiveFailures,
      resumeReady: true,
      ...extra,
    });
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      const timer = setTimeout(resolve, ms);
      // Check stop flag periodically during sleep
      const checker = setInterval(() => {
        if (this.stopRequested) {
          clearTimeout(timer);
          clearInterval(checker);
          resolve();
        }
      }, 500);
    });
  }
}
