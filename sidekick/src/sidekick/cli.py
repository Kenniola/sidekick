"""Sidekick CLI — init, serve, and list-configs commands.

Entry points:
  sidekick init          — scaffold ~/.sidekick/ and register MCP in VS Code
  sidekick serve         — run the MCP server (used by mcp.json)
  sidekick list-configs  — show available customer profiles
"""

from __future__ import annotations

import importlib.resources
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _get_user_dir() -> Path:
    """Return ~/.sidekick/."""
    import os
    return Path(os.environ.get("SIDEKICK_HOME", Path.home() / ".sidekick"))


def _get_vscode_user_settings_path() -> Path | None:
    """Find the VS Code User settings directory (cross-platform)."""
    import platform
    system = platform.system()
    if system == "Windows":
        appdata = Path.home() / "AppData" / "Roaming" / "Code" / "User"
    elif system == "Darwin":
        appdata = Path.home() / "Library" / "Application Support" / "Code" / "User"
    else:
        appdata = Path.home() / ".config" / "Code" / "User"
    return appdata if appdata.exists() else None


def _cmd_init():
    """Scaffold ~/.sidekick/ and register the MCP server in VS Code."""
    user_dir = _get_user_dir()
    print(f"Initialising Sidekick at {user_dir}\n")

    # 1. Create directory structure
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "cache").mkdir(exist_ok=True)
    (user_dir / "outputs").mkdir(exist_ok=True)
    (user_dir / "configs").mkdir(exist_ok=True)
    print(f"\u2713 Created {user_dir}/")

    # 1b. Platform checks
    _check_platform()

    # 2. Copy customers.yaml starter (if not exists)
    customers_file = user_dir / "customers.yaml"
    if not customers_file.exists():
        try:
            template_ref = importlib.resources.files("sidekick") / "configs" / "_template.yaml"
            template_content = template_ref.read_text(encoding="utf-8")
        except (FileNotFoundError, TypeError):
            template_content = (
                "# Sidekick — Customer Profiles\n"
                "# Each top-level key is a profile name.\n"
                "# Usage: @sidekick listen --config <profile>\n\n"
                "# example:\n"
                "#   customer: Example Corp\n"
                "#   description: \"Your engagement description\"\n"
                "#   participants:\n"
                "#     consultant: [\"Your Name\"]\n"
            )
        customers_file.write_text(template_content, encoding="utf-8")
        print(f"\u2713 Created {customers_file}")
    else:
        print(f"\u2713 {customers_file} already exists (kept)")

    # 3. Check for GitHub token
    gh_token = _check_github_token()

    # 4. Register MCP server in VS Code User Settings
    _register_mcp_server()

    # 5. Install sidekick-notify VS Code extension
    _install_notify_extension()

    # 6. Summary
    print("\n" + "\u2501" * 50)
    print("\u2713 Sidekick is ready!\n")
    if not gh_token:
        print("\u26a0\ufe0f  No GitHub token detected.")
        print("   Install gh CLI and run: gh auth login")
        print("   Or set GITHUB_TOKEN in your environment.\n")
    print("Next steps:")
    print(f"  1. Edit your customer profiles: {customers_file}")
    print("  2. In VS Code Copilot Chat, type:")
    print("       @sidekick listen              \u2014 start with defaults")
    print("       @sidekick listen --config acme \u2014 use a customer profile")
    print(f"\nConfig reference: {user_dir / 'customers.yaml'}")


def _check_platform():
    """Warn about platform-specific limitations."""
    import platform
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        print("\u26a0\ufe0f  ARM64 detected — Azure Speech SDK is not supported on this architecture.")
        print("   Use the Whisper backend (default with [live] extras).")
        # Check if azure SDK is actually installed
        try:
            import azure.cognitiveservices.speech  # noqa: F401
            print("   azure-cognitiveservices-speech is installed but will fail at runtime on ARM64.")
        except ImportError:
            pass


def _check_github_token() -> bool:
    """Check if a GitHub token is available."""
    import os
    if os.environ.get("GITHUB_TOKEN"):
        print("\u2713 GitHub token found (GITHUB_TOKEN env var)")
        return True

    # Try gh CLI
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            print("\u2713 GitHub token available via gh CLI")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return False


def _install_notify_extension():
    """Install the sidekick-notify VS Code extension from the bundled vsix."""
    try:
        vsix_ref = importlib.resources.files("sidekick") / "extensions" / "sidekick-notify.vsix"
        vsix_path = str(vsix_ref)
    except (FileNotFoundError, TypeError):
        print("\u26a0\ufe0f  sidekick-notify.vsix not found in package — skipping extension install")
        return

    # Check if VS Code CLI is available
    code_cmd = shutil.which("code")
    if not code_cmd:
        print("\u26a0\ufe0f  'code' CLI not found — install the extension manually:")
        print(f"   code --install-extension {vsix_path}")
        return

    try:
        result = subprocess.run(
            [code_cmd, "--install-extension", vsix_path, "--force"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print("\u2713 Installed sidekick-notify VS Code extension")
        else:
            # Extension may already be installed or VS Code returned a warning
            if "already installed" in (result.stdout + result.stderr).lower():
                print("\u2713 sidekick-notify extension already installed")
            else:
                print(f"\u26a0\ufe0f  Extension install returned: {result.stderr.strip()}")
    except (subprocess.TimeoutExpired, OSError):
        print("\u26a0\ufe0f  Could not install extension automatically")
        print(f"   Run manually: code --install-extension {vsix_path}")


def _register_mcp_server():
    """Register sidekick as an MCP server in VS Code User Settings."""
    vscode_dir = _get_vscode_user_settings_path()
    if not vscode_dir:
        print("\u26a0\ufe0f  VS Code User settings directory not found — skipping MCP registration")
        print("   Add the MCP config manually to .vscode/mcp.json in your workspace")
        return

    mcp_file = vscode_dir / "mcp.json"

    # Build the MCP server entry
    python_path = sys.executable
    server_entry = {
        "command": "powershell",
        "args": [
            "-NoProfile",
            "-Command",
            f"$env:GITHUB_TOKEN = (gh auth token); & '{python_path}' -m sidekick.server",
        ],
    }

    # Load or create mcp.json
    mcp_config: dict = {"servers": {}}
    if mcp_file.exists():
        try:
            mcp_config = json.loads(mcp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            # Backup corrupt file
            backup = mcp_file.with_suffix(".json.bak")
            shutil.copy2(mcp_file, backup)
            print(f"\u26a0\ufe0f  Backed up corrupt mcp.json to {backup}")
            mcp_config = {"servers": {}}

    if "servers" not in mcp_config:
        mcp_config["servers"] = {}

    if "sidekick" in mcp_config["servers"]:
        print(f"\u2713 MCP server already registered in {mcp_file}")
        return

    mcp_config["servers"]["sidekick"] = server_entry
    mcp_file.write_text(
        json.dumps(mcp_config, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\u2713 Registered MCP server in {mcp_file}")


def _cmd_serve():
    """Run the MCP server (called by mcp.json)."""
    import asyncio
    import logging
    from sidekick.server import server

    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
    asyncio.run(server.run_stdio_async())


def _cmd_list_configs():
    """List available customer profiles."""
    from sidekick.config import list_available_configs, get_user_dir

    profiles = list_available_configs()
    user_dir = get_user_dir()

    if not profiles:
        print(f"No customer profiles found.")
        print(f"Add profiles to: {user_dir / 'customers.yaml'}")
        return

    print(f"Available profiles ({user_dir}):\n")
    for name in profiles:
        print(f"  \u2022 {name}")
    print(f"\nUsage: @sidekick listen --config <profile>")


def _cmd_uninstall():
    """Remove all sidekick artifacts from the system."""
    user_dir = _get_user_dir()

    print("Sidekick Uninstaller")
    print("=" * 40)
    print()
    print("This will remove:")
    print(f"  1. {user_dir}/ (config, cache, outputs, session logs)")
    print(f"  2. MCP server entry from VS Code User settings")
    print(f"  3. sidekick-notify VS Code extension")
    print(f"  4. sidekick-copilot uv tool environment")
    print()

    # Check --yes flag for non-interactive use
    skip_confirm = "--yes" in sys.argv or "-y" in sys.argv

    if not skip_confirm:
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    print()

    # 1. Remove MCP entry from VS Code User settings
    _unregister_mcp_server()

    # 2. Uninstall sidekick-notify extension
    code_cmd = shutil.which("code")
    if code_cmd:
        try:
            result = subprocess.run(
                [code_cmd, "--uninstall-extension", "sidekick-notify"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                print("\u2713 Uninstalled sidekick-notify extension")
            else:
                print("\u2713 sidekick-notify extension not found (already removed)")
        except (subprocess.TimeoutExpired, OSError):
            print("\u26a0\ufe0f  Could not uninstall extension — run manually:")
            print("   code --uninstall-extension sidekick-notify")
    else:
        print("\u26a0\ufe0f  VS Code CLI not found — uninstall extension manually if installed")

    # 3. Remove ~/.sidekick/ directory
    if user_dir.exists():
        import shutil as _shutil
        _shutil.rmtree(user_dir, ignore_errors=True)
        if not user_dir.exists():
            print(f"\u2713 Removed {user_dir}/")
        else:
            print(f"\u26a0\ufe0f  Could not fully remove {user_dir}/ — delete manually")
    else:
        print(f"\u2713 {user_dir}/ not found (already removed)")

    # 4. Remove uv tool environment
    uv_cmd = shutil.which("uv")
    if uv_cmd:
        try:
            result = subprocess.run(
                [uv_cmd, "tool", "uninstall", "sidekick-copilot"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                print("\u2713 Removed sidekick-copilot uv tool environment")
            else:
                print("\u2713 sidekick-copilot not in uv tools (already removed)")
        except (subprocess.TimeoutExpired, OSError):
            print("\u26a0\ufe0f  Could not remove uv tool — run manually:")
            print("   uv tool uninstall sidekick-copilot")
    else:
        print("\u26a0\ufe0f  uv not found — if installed via pip, run: pip uninstall sidekick-copilot")

    print()
    print("\u2501" * 40)
    print("\u2713 Sidekick uninstalled.")
    print()
    print("Not removed (shared tools, remove manually if desired):")
    print("  - uv:  irm https://astral.sh/uv/uninstall.ps1 | iex")
    print("  - gh:  winget uninstall GitHub.cli")
    print()


def _unregister_mcp_server():
    """Remove sidekick entry from VS Code User mcp.json."""
    vscode_dir = _get_vscode_user_settings_path()
    if not vscode_dir:
        print("\u2713 VS Code settings not found (nothing to remove)")
        return

    mcp_file = vscode_dir / "mcp.json"
    if not mcp_file.exists():
        print("\u2713 No mcp.json found (nothing to remove)")
        return

    try:
        mcp_config = json.loads(mcp_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        print(f"\u26a0\ufe0f  Could not parse {mcp_file} — remove sidekick entry manually")
        return

    servers = mcp_config.get("servers", {})
    if "sidekick" not in servers:
        print("\u2713 No sidekick entry in mcp.json (already removed)")
        return

    del servers["sidekick"]
    mcp_file.write_text(json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8")
    print(f"\u2713 Removed sidekick from {mcp_file}")


def main():
    """CLI entry point: sidekick <command>."""
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print("Sidekick \u2014 Real-time meeting co-pilot\n")
        print("Commands:")
        print("  sidekick init          Scaffold ~/.sidekick/ and register in VS Code")
        print("  sidekick serve         Run the MCP server (used by mcp.json)")
        print("  sidekick list-configs  Show available customer profiles")
        print("  sidekick uninstall     Remove all sidekick artifacts")
        print("  sidekick help          Show this help message")
        return

    command = args[0]
    if command == "init":
        _cmd_init()
    elif command == "serve":
        _cmd_serve()
    elif command == "list-configs":
        _cmd_list_configs()
    elif command == "uninstall":
        _cmd_uninstall()
    else:
        print(f"Unknown command: {command}")
        print("Run 'sidekick help' for available commands.")
        sys.exit(1)


if __name__ == "__main__":
    main()
