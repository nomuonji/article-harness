# Windows PowerShell launcher: use absolute WSL paths, not C:\ paths.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$HarnessPath,
    [Parameter(Mandatory=$true)][string]$SitePath,
    [Parameter(Mandatory=$true)][string]$StatePath,
    [string]$Distro = '',
    [int]$Workers = 2,
    [int]$MaxJobs = 3,
    [double]$IntervalMinutes = 5,
    [string]$Model = '',
    [switch]$Once,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
foreach ($p in @($HarnessPath, $SitePath, $StatePath)) {
    if ($p -notmatch '^/' -or $p -match '[\r\n"]') {
        throw 'HarnessPath, SitePath, StatePath must be absolute WSL/Linux paths.'
    }
}
if ($Workers -lt 1 -or $Workers -gt 6 -or $MaxJobs -lt 1 -or $IntervalMinutes -lt 0) {
    throw 'Invalid run limits.'
}
$wslArgs = @()
if ($Distro) { $wslArgs += @('--distribution', $Distro) }
$wslArgs += @('--cd', $HarnessPath, '--exec', 'bash', '-lic',
              'exec bash scripts/run-local.sh "$@"', 'article-harness-local',
              '--repo', $SitePath,
              '--state', $StatePath,
              '--workers', "$Workers", '--max-jobs', "$MaxJobs",
              '--interval-minutes', "$IntervalMinutes")
if ($Model) { $wslArgs += @('--model', $Model) }
if ($Once) { $wslArgs += '--once' }
if ($DryRun) { $wslArgs += '--dry-run' }
& wsl.exe @wslArgs
exit $LASTEXITCODE
