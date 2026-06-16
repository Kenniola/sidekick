import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

const ALERTS_PATH = path.join(
  os.homedir(),
  ".sidekick",
  "live",
  "alerts.jsonl"
);
const POLL_MS = 2000;

let pollTimer: ReturnType<typeof setInterval> | undefined;
let lastSize = 0;
let statusBar: vscode.StatusBarItem;
let unseenCount = 0;

// ── Lifecycle ──────────────────────────────────────────────────────────

export function activate(ctx: vscode.ExtensionContext): void {
  // Skip old alerts — start from current file end
  try {
    lastSize = fs.statSync(ALERTS_PATH).size;
  } catch {
    lastSize = 0;
  }

  // Status-bar button (right side)
  statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    200
  );
  statusBar.command = "sidekick-notify.showStatus";
  setIdle();
  statusBar.show();
  ctx.subscriptions.push(statusBar);

  // "Show Status" command — opens Copilot Chat with @sidekick status
  ctx.subscriptions.push(
    vscode.commands.registerCommand("sidekick-notify.showStatus", () => {
      unseenCount = 0;
      setIdle();
      vscode.commands.executeCommand("workbench.action.chat.open", {
        query: "@sidekick status",
        isPartialQuery: false,
      });
    })
  );

  // Poll the alerts file for new lines
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
    // File was truncated (new session) — reset
    lastSize = 0;
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
    for (const line of buf.split("\n")) {
      if (!line.trim()) {
        continue;
      }
      try {
        showAlert(JSON.parse(line));
      } catch {
        // skip malformed lines
      }
    }
  });
}

// ── Toast notifications ────────────────────────────────────────────────

interface Alert {
  type: string;
  summary: string;
  answer?: string;
  source?: string;
  file?: string;
  confidence: string;
  priority: string;
  timestamp: string;
}

function showAlert(alert: Alert): void {
  const icon: Record<string, string> = {
    research: "🔍",
    prototype: "🛠️",
    roadmap: "🗺️",
    deliverables: "📦",
  };
  const emoji = icon[alert.type] ?? "📋";
  // Prefer the one-line answer (the "answer card"); fall back to the summary.
  const headline = (alert.answer && alert.answer.trim()) || alert.summary;
  const msg = `${emoji} Sidekick: ${headline}`;

  // High-priority findings get a warning toast (orange); others get info (blue)
  const isHigh = alert.priority === "high" || alert.confidence === "high";
  const show = isHigh
    ? vscode.window.showWarningMessage
    : vscode.window.showInformationMessage;

  // Offer "Open Source" when a URL is present, "Open File" for a saved
  // deliverables/artifact path (e.g. the post-call deliverables pack).
  const hasSource = !!(alert.source && /^https?:\/\//.test(alert.source));
  const hasFile = !!(alert.file && alert.file.trim());
  const actions: string[] = [];
  if (hasSource) {
    actions.push("Open Source");
  }
  if (hasFile) {
    actions.push("Open File");
  }
  actions.push("View in Chat");

  show(msg, ...actions).then((choice) => {
    if (choice === "Open Source" && alert.source) {
      vscode.env.openExternal(vscode.Uri.parse(alert.source));
      return;
    }
    if (choice === "Open File" && alert.file) {
      vscode.commands.executeCommand(
        "vscode.open",
        vscode.Uri.file(alert.file)
      );
      return;
    }
    if (choice === "View in Chat") {
      unseenCount = 0;
      setIdle();
      vscode.commands.executeCommand("workbench.action.chat.open", {
        query: "@sidekick status",
        isPartialQuery: false,
      });
    }
  });

  // Update status bar badge
  unseenCount++;
  statusBar.text = `$(megaphone) Sidekick (${unseenCount})`;
  statusBar.backgroundColor = new vscode.ThemeColor(
    isHigh
      ? "statusBarItem.warningBackground"
      : "statusBarItem.prominentBackground"
  );
  statusBar.tooltip = `${unseenCount} unseen Sidekick finding(s) — click to view`;
}

function setIdle(): void {
  statusBar.text = "$(megaphone) Sidekick";
  statusBar.backgroundColor = undefined;
  statusBar.tooltip = "Sidekick — click for status";
}
