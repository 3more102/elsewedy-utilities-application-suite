$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$repo = "https://github.com/3more102/elsewedy-utilities-application-suite.git"

Write-Host "EUAS v3.9.0 - Publish to GitHub" -ForegroundColor Cyan
Write-Host "Target: $repo"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is not installed or not available in PATH. Install Git for Windows first."
}

if (-not (Test-Path ".git")) { git init }
git branch -M main

if (-not (git config user.name)) { git config user.name "EUAS Developers" }
if (-not (git config user.email)) { git config user.email "euas@local.invalid" }

git add -A
$pending = git diff --cached --name-only
if ($pending) { git commit -m "Initial EUAS v3.9.0 release" }

$origin = git remote get-url origin 2>$null
if ($LASTEXITCODE -ne 0) { git remote add origin $repo } else { git remote set-url origin $repo }

git push -u origin main
if ($LASTEXITCODE -ne 0) { throw "git push failed" }

Write-Host "Published successfully:" -ForegroundColor Green
Write-Host "https://github.com/3more102/elsewedy-utilities-application-suite"
