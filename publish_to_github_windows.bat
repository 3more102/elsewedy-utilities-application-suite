@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  EUAS v3.9.0 - Publish to GitHub
echo  Repository: 3more102/elsewedy-utilities-application-suite
echo ============================================================
echo.

where git >nul 2>nul
if errorlevel 1 (
  echo ERROR: Git is not installed or not available in PATH.
  echo Install Git for Windows from https://git-scm.com/download/win then run this file again.
  pause
  exit /b 1
)

if not exist .git (
  git init
  if errorlevel 1 goto :fail
)

git branch -M main

rem Configure a local commit identity only if none is already configured.
git config user.name >nul 2>nul
if errorlevel 1 git config user.name "EUAS Developers"
git config user.email >nul 2>nul
if errorlevel 1 git config user.email "euas@local.invalid"

git add -A

git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Initial EUAS v3.9.0 release"
  if errorlevel 1 goto :fail
) else (
  echo No new local changes to commit.
)

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  git remote add origin https://github.com/3more102/elsewedy-utilities-application-suite.git
) else (
  git remote set-url origin https://github.com/3more102/elsewedy-utilities-application-suite.git
)

echo.
echo Pushing EUAS to GitHub...
echo If GitHub asks you to sign in, complete the browser sign-in window.
echo.
git push -u origin main
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo SUCCESS: EUAS v3.9.0 is now on GitHub.
echo https://github.com/3more102/elsewedy-utilities-application-suite
echo ============================================================
pause
exit /b 0

:fail
echo.
echo ERROR: Publish failed. Read the Git error above.
echo Nothing in your EUAS source files was deleted.
pause
exit /b 1
