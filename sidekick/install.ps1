# Sidekick — Windows Bootstrap Installer
# One-liner: irm https://raw.githubusercontent.com/Kenniola/sidekick/main/sidekick/install.ps1 | iex
#
# What this does:
#   1. Installs uv (fast Python package manager) if missing
#   2. Installs sidekick-copilot into an isolated uv tool environment
#   3. Runs `sidekick init` to scaffold config, register MCP, install VS Code extension

param(
    # Install extras: 'live' (default, Whisper), 'azure' (Azure Speech SDK), 'all'
    [ValidateSet('live', 'azure', 'all')]
    [string]$Features = 'live'
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== Sidekick Installer ===" -ForegroundColor Cyan
Write-Host ""

# --- ARM64 compatibility check ---
$arch = $env:PROCESSOR_ARCHITECTURE
$SkipExtras = $false
if ($arch -eq 'ARM64') {
    if ($Features -in @('azure', 'all')) {
        Write-Host "[!] Azure Speech SDK does not support Windows ARM64." -ForegroundColor Red
    }
    if ($Features -in @('live', 'azure', 'all')) {
        Write-Host "[!] PyAudioWPatch has no ARM64 wheels - live audio capture unavailable." -ForegroundColor Yellow
        Write-Host "    Installing base package. Research, offerings, prototyping still work." -ForegroundColor Yellow
        $SkipExtras = $true
    }
} elseif ($Features -in @('azure', 'all')) {
    Write-Host "[i] Azure Speech SDK selected - requires an Azure Speech resource." -ForegroundColor Yellow
    Write-Host "    Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in ~/.sidekick/customers.yaml" -ForegroundColor Yellow
}

# --- Step 1: Ensure uv is installed ---
$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    Write-Host "Installing uv (Python package manager)..." -ForegroundColor Yellow
    irm https://astral.sh/uv/install.ps1 | iex
    # Refresh PATH so uv is available immediately
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                 [System.Environment]::GetEnvironmentVariable("PATH", "User") + ";" +
                 "$env:USERPROFILE\.local\bin"
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCmd) {
        Write-Host "[X] uv install failed. Install manually: https://docs.astral.sh/uv/" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] uv installed" -ForegroundColor Green
} else {
    Write-Host "[OK] uv found: $(uv --version)" -ForegroundColor Green
}

# --- Step 2: Ensure GitHub CLI is installed ---
$ghCmd = Get-Command gh -ErrorAction SilentlyContinue
if (-not $ghCmd) {
    Write-Host "Installing GitHub CLI..." -ForegroundColor Yellow
    $hasWinget = Get-Command winget -ErrorAction SilentlyContinue
    if ($hasWinget) {
        winget install --id GitHub.cli --accept-package-agreements --accept-source-agreements
        # Refresh PATH
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                     [System.Environment]::GetEnvironmentVariable("PATH", "User")
        $ghCmd = Get-Command gh -ErrorAction SilentlyContinue
    }
    if (-not $ghCmd) {
        Write-Host "[X] Could not install GitHub CLI automatically." -ForegroundColor Red
        Write-Host "    Install from: https://cli.github.com" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "[OK] GitHub CLI installed" -ForegroundColor Green
    Write-Host ""
    Write-Host "[!] Run 'gh auth login' now to authenticate, then re-run this installer." -ForegroundColor Yellow
    exit 0
} else {
    # Check if authenticated
    $ghToken = gh auth token 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] GitHub CLI found but not logged in." -ForegroundColor Yellow
        Write-Host "    Run 'gh auth login' first, then re-run this installer." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "[OK] GitHub CLI authenticated" -ForegroundColor Green
}

# --- Step 3: Install sidekick via uv tool ---
# TODO: Replace with actual repo URL or PyPI name when decided
$RepoUrl = "git+https://github.com/Kenniola/sidekick.git#subdirectory=sidekick"

Write-Host "Installing sidekick-copilot$(if (-not $SkipExtras) { "[$Features]" })..."
$installPkg = if (-not $SkipExtras) { "sidekick-copilot[$Features] @ $RepoUrl" } else { "sidekick-copilot @ $RepoUrl" }
uv tool install $installPkg --force
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] uv tool install failed" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] sidekick-copilot installed" -ForegroundColor Green

# --- Step 4: Run sidekick init ---
Write-Host ""
sidekick init

Write-Host ""
Write-Host "=== Installation complete ===" -ForegroundColor Cyan
Write-Host ""
