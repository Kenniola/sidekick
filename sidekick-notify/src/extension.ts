import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { FeedModel, FeedEntry, Alert, isHighPriority } from "./feedModel";

const ALERTS_PATH = path.join(
  os.homedir(),
  ".sidekick",
  "live",
  "alerts.jsonl"
);
const POLL_MS = 2000;
const STALE_MS = 10 * 60 * 1000; // findings older than 10 min render dimmed

let pollTimer: ReturnType<typeof setInterval> | undefined;
let lastSize = 0;
let statusBar: vscode.StatusBarItem;
const model = new FeedModel();
let feedProvider: SidekickFeedProvider;

// ── Feed TreeView (B2) ─────────────────────────────────────────────────

class SidekickFeedProvider implements vscode.TreeDataProvider<FeedEntry> {
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  refresh(): void {
    this._onDidChange.fire();
  }

  getTreeItem(entry: FeedEntry): vscode.TreeItem {
    const item = new vscode.TreeItem(
      entry.headline,
      vscode.TreeItemCollapsibleState.None
    );

    const iconId: Record<string, string> = {
      research: "search",
      prototype: "tools",
      roadmap: "map",
      sizing: "graph",
      diagnostic: "pulse",
      deliverables: "package",
    };
    const stale = model.isStale(entry, Date.now(), STALE_MS);
    const highlight = isHighPriority(entry) && !entry.seen && !entry.superseded;
    item.iconPath = new vscode.ThemeIcon(
      iconId[entry.type] ?? "note",
      highlight ? new vscode.ThemeColor("charts.orange") : undefined
    );

    const rel = relativeTime(entry.timestamp);
    const tags = [entry.type, rel];
    if (entry.superseded) {
      tags.push("superseded");
    } else if (stale) {
      tags.push("stale");
    }
    item.description = tags.join(" · ");

    const md = new vscode.MarkdownString();
    md.appendMarkdown(`**${entry.headline}**\n\n`);
    if (entry.rationale) {
      md.appendMarkdown(`_${entry.rationale}_\n\n`);
    }
    if (entry.source) {
      md.appendMarkdown(`[Open source](${entry.source})\n\n`);
    }
    if (entry.file) {
      md.appendMarkdown(`File: \`${entry.file}\`\n\n`);
    }
    md.appendMarkdown(`priority: ${entry.priority} · ${rel}`);
    item.tooltip = md;

    // Click → open the most useful target for this finding.
    if (entry.source && /^https?:\/\//.test(entry.source)) {
      item.command = {
        command: "vscode.open",
        title: "Open Source",
        arguments: [vscode.Uri.parse(entry.source)],
      };
    } else if (entry.file) {
      item.command = {
        command: "vscode.open",
        title: "Open File",
        arguments: [vscode.Uri.file(entry.file)],
      };
    } else {
      item.command = {
        command: "sidekick-notify.showStatus",
        title: "View in Chat",
      };
    }
    return item;
  }

  getChildren(): FeedEntry[] {
    return model.entries;
  }
}

// ── Lifecycle ──────────────────────────────────────────────────────────

export function activate(ctx: vscode.ExtensionContext): void {
  // Skip old alerts — start from the current file end.
  try {
    lastSize = fs.statSync(ALERTS_PATH).size;
  } catch {
    lastSize = 0;
  }

  statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    200
  );
  statusBar.command = "sidekick-notify.openFeed";
  setIdle();
  statusBar.show();
  ctx.subscriptions.push(statusBar);

  feedProvider = new SidekickFeedProvider();
  ctx.subscriptions.push(
    vscode.window.createTreeView("sidekickFeed", {
      treeDataProvider: feedProvider,
    })
  );

  ctx.subscriptions.push(
    vscode.commands.registerCommand("sidekick-notify.showStatus", () => {
      model.markAllSeen();
      feedProvider.refresh();
      updateBadge();
      vscode.commands.executeCommand("workbench.action.chat.open", {
        query: "@sidekick status",
        isPartialQuery: false,
      });
    }),
    vscode.commands.registerCommand("sidekick-notify.openFeed", () => {
      model.markAllSeen();
      feedProvider.refresh();
      updateBadge();
      vscode.commands.executeCommand("sidekickFeed.focus");
    }),
    vscode.commands.registerCommand("sidekick-notify.clearSeen", () => {
      model.markAllSeen();
      feedProvider.refresh();
      updateBadge();
    })
  );

  pollTimer = setInterval(poll, POLL_MS);
  ctx.subscriptions.push({
    dispose: () => {
      if (pollTimer) {
        clearInterval(pollTimer);
      }
    },
  });
}

export function deactivate(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
  }
}

// ── File polling ───────────────────────────────────────────────────────

function poll(): void {
  let stat: fs.Stats;
  try {
    stat = fs.statSync(ALERTS_PATH);
  } catch {
    return; // file doesn't exist yet — Sidekick hasn't started
  }

  if (stat.size < lastSize) {
    lastSize = 0; // file truncated (new session) — reset
  }
  if (stat.size <= lastSize) {
    return; // no new data
  }

  const stream = fs.createReadStream(ALERTS_PATH, {
    start: lastSize,
    encoding: "utf-8",
  });

  let buf = "";
  stream.on("data", (chunk) => {
    buf += String(chunk);
  });
  stream.on("end", () => {
    lastSize = stat.size;
    let touched = false;
    for (const line of buf.split("\n")) {
      if (!line.trim()) {
        continue;
      }
      try {
        handleAlert(JSON.parse(line) as Alert);
        touched = true;
      } catch {
        // skip malformed lines
      }
    }
    if (touched) {
      feedProvider.refresh();
      updateBadge();
    }
  });
}

// ── Alert handling ─────────────────────────────────────────────────────

function handleAlert(alert: Alert): void {
  const { isNew } = model.addAlert(alert);
  // Gate toasts (B1): only critical/high, and only for genuinely new findings
  // (an update to an existing id refreshes the feed row without re-toasting).
  if (model.shouldToast(alert) && isNew) {
    showToast(alert);
  }
}

function showToast(alert: Alert): void {
  const icon: Record<string, string> = {
    research: "🔍",
    prototype: "🛠️",
    roadmap: "🗺️",
    deliverables: "📦",
  };
  const emoji = icon[alert.type] ?? "📋";
  const headline = (alert.answer && alert.answer.trim()) || alert.summary;
  const msg = `${emoji} Sidekick: ${headline}`;

  const hasSource = !!(alert.source && /^https?:\/\//.test(alert.source));
  const hasFile = !!(alert.file && alert.file.trim());
  const actions: string[] = [];
  if (hasSource) {
    actions.push("Open Source");
  }
  if (hasFile) {
    actions.push("Open File");
  }
  actions.push("Open Feed");

  vscode.window.showWarningMessage(msg, ...actions).then((choice) => {
    if (choice === "Open Source" && alert.source) {
      vscode.env.openExternal(vscode.Uri.parse(alert.source));
    } else if (choice === "Open File" && alert.file) {
      vscode.commands.executeCommand("vscode.open", vscode.Uri.file(alert.file));
    } else if (choice === "Open Feed") {
      vscode.commands.executeCommand("sidekick-notify.openFeed");
    }
  });
}

// ── Status bar ─────────────────────────────────────────────────────────

function updateBadge(): void {
  const n = model.unseenHighCount();
  if (n > 0) {
    statusBar.text = `$(megaphone) Sidekick (${n})`;
    statusBar.backgroundColor = new vscode.ThemeColor(
      "statusBarItem.warningBackground"
    );
    statusBar.tooltip = `${n} unseen high-priority finding(s) — click to open the feed`;
  } else {
    setIdle();
  }
}

function setIdle(): void {
  statusBar.text = "$(megaphone) Sidekick";
  statusBar.backgroundColor = undefined;
  statusBar.tooltip = "Sidekick — click to open the feed";
}

function relativeTime(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) {
    return "";
  }
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) {
    return `${s}s ago`;
  }
  const m = Math.floor(s / 60);
  if (m < 60) {
    return `${m}m ago`;
  }
  const h = Math.floor(m / 60);
  return `${h}h ago`;
}
