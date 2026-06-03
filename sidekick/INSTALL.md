# Sidekick — Installation Guide

## Prerequisites

- **Windows 10 or 11**
- **VS Code** with the **GitHub Copilot Chat** extension
- **GitHub Copilot license** (via your GitHub account)

That's it. Python, GitHub CLI, and Azure resources are all installed or handled automatically.

---

## Install

### Step 1 — Open PowerShell

Press `Win + X` → select **Terminal** (or **Windows PowerShell**).
Alternatively, press `Win + R`, type `powershell`, press Enter.

### Step 2 — Paste this single line and press Enter

```powershell
irm https://raw.githubusercontent.com/Kenniola/sidekick/main/sidekick/install.ps1 | iex
```

The installer will:
- Install **uv** (Python package manager) and **Python** — if not already present
- Install **GitHub CLI** — if not already present (you'll be asked to run `gh auth login`, then re-run the line above)
- Install **sidekick-copilot** and all dependencies
- Create your config at `~/.sidekick/`
- Register the MCP server in VS Code
- Install the notification extension

### Step 3 — Edit your profile

Open the file `%USERPROFILE%\.sidekick\customers.yaml` in any text editor and add your name:

```yaml
myproject:
  customer: Acme Corp
  description: "Data platform migration"
  participants:
    consultant: ["Your Name"]
```

### Step 4 — Use it

Open **VS Code** → open **Copilot Chat** (`Ctrl+Shift+I`) → type:

```
@sidekick listen
@sidekick listen --config myproject
```

---

## Verify (optional)

Paste these into PowerShell to confirm everything is working:

```powershell
sidekick --help                                                    # CLI works
Get-Content "$env:APPDATA\Code\User\mcp.json" | Select-String sidekick  # MCP registered
code --list-extensions | Select-String sidekick                    # Extension installed
gh auth status                                                     # GitHub token OK
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `sidekick` not found | Close and reopen terminal to refresh PATH |
| MCP server not showing in VS Code | Restart VS Code; check `%APPDATA%/Code/User/mcp.json` has a `sidekick` entry |
| No audio captured | Check system audio is playing through default output device — Sidekick uses loopback capture |
| `gh auth token` fails | Run `gh auth login` and select HTTPS + browser auth |
| ARM64 + Azure Speech | Not supported — use Whisper (default). Installer auto-detects and warns |
| Extension not installed | Run manually: `code --install-extension <path-to-vsix>` — path shown in `sidekick init` output |

---

## Uninstall

### Automated (recommended)

```powershell
sidekick uninstall
```

This removes:
- `~/.sidekick/` — config, cache, session outputs, live alerts
- MCP server entry from `%APPDATA%/Code/User/mcp.json`
- sidekick-notify VS Code extension
- sidekick-copilot uv tool environment

Add `-y` to skip the confirmation prompt:

```powershell
sidekick uninstall -y
```

### Manual (if CLI is already gone)

```powershell
# 1. Remove user data
Remove-Item "$env:USERPROFILE\.sidekick" -Recurse -Force

# 2. Remove uv tool environment
uv tool uninstall sidekick-copilot

# 3. Remove MCP entry — edit %APPDATA%/Code/User/mcp.json
#    Delete the "sidekick" key from "servers"

# 4. Remove VS Code extension
code --uninstall-extension sidekick-notify
```

### Optional: remove shared tools

These are shared by other projects — only remove if you no longer need them:

```powershell
# Remove uv (Python package manager)
irm https://astral.sh/uv/uninstall.ps1 | iex

# Remove GitHub CLI
winget uninstall GitHub.cli
```

### Artifact reference

| Artifact | Location |
|----------|----------|
| Config & data | `~/.sidekick/` |
| Customer profiles | `~/.sidekick/customers.yaml` |
| Session outputs | `~/.sidekick/outputs/<customer>/` |
| Live alerts | `~/.sidekick/live/alerts.jsonl` |
| Cache | `~/.sidekick/cache/` |
| MCP registration | `%APPDATA%/Code/User/mcp.json` |
| VS Code extension | sidekick-notify |
| uv tool env | `uv tool dir` (run to see path) |
