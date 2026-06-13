param(
  [switch]$DryRun,
  [switch]$PostCurrent,
  [switch]$Once,
  [switch]$AutostartNwws
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if ($DryRun) { $env:SPC_DRY_RUN = "1" }
if ($PostCurrent) { $env:SPC_POST_CURRENT_ON_START = "1" }
if ($AutostartNwws) { $env:NWWS_AUTOSTART = "1" }

$argsList = @()
if ($Once) { $argsList += "--once" }

python .\spc_outlook_bot.py @argsList
