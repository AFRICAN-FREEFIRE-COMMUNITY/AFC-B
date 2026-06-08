<#
register_schedule.ps1
================================================================================
Registers the weekly AFC OCR training-cycle task with Windows Task Scheduler.

WHAT IT DOES
  1. Resolves the absolute paths this machine needs (the backend venv python and the
     backend repo dir) so the operator does not hand-edit the XML.
  2. Loads afc_ocr_train_cycle.xml (sitting next to this script), substitutes the
     {{PYTHON_EXE}} / {{WORKING_DIR}} / {{USER_ID}} placeholders, and registers the task
     under \AFC\afc_ocr_train_cycle via Register-ScheduledTask.
  3. Prints how to verify (schtasks /Query) and how to run it once by hand.

WHY A SCRIPT (not just "import the XML")
  The XML carries machine-specific paths the operator should not have to edit. This
  script fills them from the live environment so registration is one command.

USAGE (from a normal PowerShell prompt; no admin needed for an InteractiveToken task in
the current user's own task folder, but run "as administrator" if your policy requires
writing under \AFC):
    cd <repo>\backend\afc_ocr\training
    .\register_schedule.ps1

  Optional overrides:
    .\register_schedule.ps1 -PythonExe "C:\path\to\python.exe" -WorkingDir "C:\path\to\backend"

  To remove the task later:
    Unregister-ScheduledTask -TaskName "afc_ocr_train_cycle" -TaskPath "\AFC\" -Confirm:$false

NOTE: This script does NOT set AFC_API_BASE / AFC_OCR_TOKEN. Set those as USER
environment variables first (see setup_windows_schedule.md) so the scheduled run can
reach the API; the task reads them at run time and they never live in the task XML.
#>

[CmdletBinding()]
param(
    # Absolute path to the TRAINING venv python. Default: derived from this script's location
    # (this script lives at backend\afc_ocr\training\, so it is ..\..\..\.venv-train\...).
    # NOTE: this is .venv-train (the separate Paddle GPU training env), NOT .venv (the serving
    # env). Paddle pins an older numpy that would break onnxruntime/opencv in the serving env,
    # so training runs in its own venv. train_cycle.py talks to the server over HTTP (requests)
    # and does not need Django, so .venv-train only needs paddlepaddle-gpu + paddleocr +
    # paddle2onnx + requests.
    [string]$PythonExe,

    # The backend repo dir (the working directory for `-m afc_ocr.training.train_cycle`).
    [string]$WorkingDir,

    # The account the task runs as. Default: the current user (DOMAIN\user or PC\user).
    [string]$UserId = "$env:USERDOMAIN\$env:USERNAME",

    # Task name + folder.
    [string]$TaskName = "afc_ocr_train_cycle",
    [string]$TaskPath = "\AFC\"
)

$ErrorActionPreference = "Stop"

# ── Resolve paths relative to this script if not passed in ──────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# backend\afc_ocr\training -> backend is three levels up.
$BackendDir = (Resolve-Path (Join-Path $ScriptDir "..\..\..")).Path

if (-not $WorkingDir) { $WorkingDir = $BackendDir }
if (-not $PythonExe)  { $PythonExe  = Join-Path $BackendDir ".venv-train\Scripts\python.exe" }

# ── Sanity-check the resolved paths so a bad setup fails here, not silently at 03:00 ──
if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at '$PythonExe'. Pass -PythonExe with the backend venv python.exe path."
}
if (-not (Test-Path $WorkingDir)) {
    Write-Error "Working dir not found at '$WorkingDir'. Pass -WorkingDir with the backend repo path."
}

$XmlTemplate = Join-Path $ScriptDir "afc_ocr_train_cycle.xml"
if (-not (Test-Path $XmlTemplate)) {
    Write-Error "Task XML not found at '$XmlTemplate' (expected next to this script)."
}

Write-Host "Registering scheduled task:"
Write-Host "  TaskName    : $TaskPath$TaskName"
Write-Host "  Python      : $PythonExe"
Write-Host "  WorkingDir  : $WorkingDir"
Write-Host "  Runs as     : $UserId"

# ── Fill the placeholders in the XML ────────────────────────────────────────────────
$xml = Get-Content -Path $XmlTemplate -Raw
$xml = $xml.Replace("{{PYTHON_EXE}}",  $PythonExe)
$xml = $xml.Replace("{{WORKING_DIR}}", $WorkingDir)
$xml = $xml.Replace("{{USER_ID}}",     $UserId)

# ── Register (replace any existing task of the same name) ───────────────────────────
# Register-ScheduledTask -Xml takes the full task definition string. -Force replaces an
# existing task with the same name/path so re-running this script is idempotent.
Register-ScheduledTask `
    -TaskName $TaskName `
    -TaskPath $TaskPath `
    -Xml $xml `
    -Force | Out-Null

Write-Host ""
Write-Host "Registered. Verify with:"
Write-Host "  schtasks /Query /TN `"$TaskPath$TaskName`" /V /FO LIST"
Write-Host ""
Write-Host "Run it once by hand (the loop will report 'not due' unless data has accrued):"
Write-Host "  Start-ScheduledTask -TaskName `"$TaskName`" -TaskPath `"$TaskPath`""
Write-Host ""
Write-Host "Or test the cycle directly without the scheduler:"
Write-Host "  & `"$PythonExe`" -m afc_ocr.training.train_cycle --force --dry-run"
