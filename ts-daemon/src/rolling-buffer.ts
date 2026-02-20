/** Char-capped deque for compaction recovery — stores recent stream output. */
export class RollingBuffer {
  private readonly maxChars: number;
  private chunks: string[] = [];
  private totalChars = 0;

  constructor(maxTokens: number) {
    this.maxChars = maxTokens * 4;
  }

  append(text: string): void {
    if (!text) return;
    this.chunks.push(text);
    this.totalChars += text.length;
    while (this.totalChars > this.maxChars && this.chunks.length > 0) {
      const removed = this.chunks.shift()!;
      this.totalChars -= removed.length;
    }
  }

  snapshot(): string {
    return this.chunks.join("");
  }

  get length(): number {
    return this.totalChars;
  }
}
