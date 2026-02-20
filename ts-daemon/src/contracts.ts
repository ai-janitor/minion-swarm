import { readFileSync } from "fs";
import { resolve } from "path";

const DOCS_DIR = resolve(
  process.env.MINION_DOCS_DIR || resolve(process.env.HOME || "~", ".minion_work", "docs")
);

/** Load a contract JSON from {docs_dir}/contracts/{name}.json. Returns null if missing. */
export function loadContract(name: string): Record<string, any> | null {
  try {
    const raw = readFileSync(resolve(DOCS_DIR, "contracts", `${name}.json`), "utf8");
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
