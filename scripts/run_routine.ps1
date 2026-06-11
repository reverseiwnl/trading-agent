# run_routine.ps1 — invoked by Windows Task Scheduler (weekdays 7:00 AM CT).
#
# Extracts the routine prompt VERBATIM from ROUTINE_PROMPT.md (the text between
# the first two "---" marker lines) and runs it through headless Claude Code.
# The prompt is piped via stdin so no quoting can mangle it. All output lands
# in logs/routine_<date>.log; the task's exit code is claude's exit code.
#
# Permissions: the headless agent gets only what .claude/settings.local.json
# allows (the routine's own scripts, web search) plus acceptEdits for repo
# file writes. It does NOT get a blanket permission bypass.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$lines = Get-Content (Join-Path $repo "ROUTINE_PROMPT.md") -Encoding UTF8
$markers = @(0..($lines.Count - 1) | Where-Object { $lines[$_].Trim() -eq "---" })
if ($markers.Count -lt 2) {
    Write-Error "ROUTINE_PROMPT.md: expected two '---' markers delimiting the prompt"
    exit 2
}
$prompt = ($lines[($markers[0] + 1)..($markers[1] - 1)] -join "`n").Trim()

New-Item -ItemType Directory -Force (Join-Path $repo "logs") | Out-Null
$log = Join-Path $repo ("logs\routine_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
Add-Content -Path $log -Value ("=== routine start {0} ===" -f (Get-Date -Format "o")) -Encoding UTF8

$claude = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
$output = $prompt | & $claude -p --permission-mode acceptEdits 2>&1 | Out-String
$code = $LASTEXITCODE

Add-Content -Path $log -Value $output -Encoding UTF8
Add-Content -Path $log -Value ("=== routine end {0} exit {1} ===" -f (Get-Date -Format "o"), $code) -Encoding UTF8
exit $code
