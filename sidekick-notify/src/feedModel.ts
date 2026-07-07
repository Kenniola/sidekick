// feedModel.ts — pure, VS Code-free logic for the Sidekick Feed (Phase 3 / B5).
//
// Kept free of any `vscode` import so it can be unit-tested with `node --test`.
// The extension glue in extension.ts renders these entries in a TreeView and
// decides when to raise a toast.

export interface Alert {
  type: string;
  summary: string;
  answer?: string;
  source?: string;
  file?: string;
  confidence: string;
  priority: string;
  timestamp: string;
  id?: string;
  thread_id?: string;
  rationale?: string;
}

export interface FeedEntry {
  key: string;
  threadId: string;
  type: string;
  priority: string;
  confidence: string;
  headline: string;
  rationale: string;
  source: string;
  file: string;
  timestamp: string;
  seen: boolean;
  superseded: boolean;
}

const MAX_ENTRIES = 200;

/** Stable key for supersede/dedup — prefers the server-provided id. */
export function alertKey(alert: Alert): string {
  if (alert.id && alert.id.trim()) {
    return alert.id.trim();
  }
  return `${alert.type}:${(alert.summary || "").slice(0, 40)}`;
}

/** Toast floor (decision #4): only critical/high are worth interrupting for. */
export function isHighPriority(x: { priority?: string; confidence?: string }): boolean {
  return x.priority === "critical" || x.priority === "high" || x.confidence === "high";
}

export function headlineOf(alert: Alert): string {
  const answer = (alert.answer || "").trim();
  return answer || alert.summary || "(finding)";
}

export class FeedModel {
  entries: FeedEntry[] = [];

  /**
   * Add a new entry or update an existing one with the same key in place.
   * A newer entry on the same thread marks older thread entries superseded.
   * Returns whether the entry was genuinely new (used to dedup toasts).
   */
  addAlert(alert: Alert): { entry: FeedEntry; isNew: boolean } {
    const key = alertKey(alert);
    const entry: FeedEntry = {
      key,
      threadId: (alert.thread_id || "").trim(),
      type: alert.type,
      priority: alert.priority || "medium",
      confidence: alert.confidence || "medium",
      headline: headlineOf(alert),
      rationale: (alert.rationale || "").trim(),
      source: alert.source || "",
      file: alert.file || "",
      timestamp: alert.timestamp || new Date().toISOString(),
      seen: false,
      superseded: false,
    };

    const existingIdx = this.entries.findIndex((e) => e.key === key);
    const isNew = existingIdx < 0;
    if (!isNew) {
      // Supersede in place: drop the stale copy, re-add at the front.
      this.entries.splice(existingIdx, 1);
    }
    this.entries.unshift(entry);

    // Thread-level supersede: a newer answer dims older ones on the same thread.
    if (entry.threadId) {
      for (const e of this.entries) {
        if (e !== entry && e.threadId === entry.threadId) {
          e.superseded = true;
        }
      }
    }

    if (this.entries.length > MAX_ENTRIES) {
      this.entries.length = MAX_ENTRIES;
    }
    return { entry, isNew };
  }

  /** Toast only critical/high — everything else lives silently in the feed. */
  shouldToast(alert: Alert): boolean {
    return isHighPriority(alert);
  }

  unseenHighCount(): number {
    return this.entries.filter(
      (e) => !e.seen && !e.superseded && isHighPriority(e),
    ).length;
  }

  markAllSeen(): void {
    for (const e of this.entries) {
      e.seen = true;
    }
  }

  /** True when an entry is older than ttlMs relative to nowMs (epoch ms). */
  isStale(entry: FeedEntry, nowMs: number, ttlMs: number): boolean {
    const t = Date.parse(entry.timestamp);
    if (Number.isNaN(t)) {
      return false;
    }
    return nowMs - t > ttlMs;
  }
}
