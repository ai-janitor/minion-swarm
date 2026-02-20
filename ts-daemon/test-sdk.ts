/**
 * Phase 0 POC — validate SDK + OAuth token works without an API key.
 * Run: npx tsx test-sdk.ts
 */
import { query, type SDKMessage } from "@anthropic-ai/claude-code";

async function main() {
  if (!process.env.CLAUDE_CODE_OAUTH_TOKEN) {
    console.error("CLAUDE_CODE_OAUTH_TOKEN not set. Run: claude setup-token");
    process.exit(1);
  }

  console.log("SDK POC: sending prompt via OAuth token...\n");

  const stream = query({
    prompt: "List files in the current directory using the Bash tool. Just run ls and show the output.",
    options: {
      permissionMode: "bypassPermissions",
      maxTurns: 3,
      pathToClaudeCodeExecutable: process.env.HOME + "/.local/bin/claude",
    },
  });

  for await (const msg of stream) {
    switch (msg.type) {
      case "system":
        if (msg.subtype === "init") {
          console.log(`[system] model=${msg.model} tools=${msg.tools.length} mode=${msg.permissionMode}`);
        }
        break;
      case "assistant":
        for (const block of msg.message.content) {
          if (block.type === "text") {
            console.log(`[assistant] ${block.text.slice(0, 200)}`);
          } else if (block.type === "tool_use") {
            console.log(`[tool_use] ${block.name}: ${JSON.stringify(block.input).slice(0, 200)}`);
          }
        }
        break;
      case "result":
        console.log(`\n[result] subtype=${msg.subtype} turns=${msg.num_turns} cost=$${msg.total_cost_usd.toFixed(4)}`);
        console.log(`[usage] input=${msg.usage.input_tokens} output=${msg.usage.output_tokens}`);
        for (const [model, usage] of Object.entries(msg.modelUsage)) {
          console.log(`[model] ${model}: ctx_window=${usage.contextWindow} in=${usage.inputTokens} out=${usage.outputTokens}`);
        }
        if (msg.subtype === "success") {
          console.log(`[text] ${msg.result.slice(0, 300)}`);
        }
        break;
    }
  }

  console.log("\nPOC passed — SDK + OAuth works.");
}

main().catch((err) => {
  console.error("POC failed:", err);
  process.exit(1);
});
