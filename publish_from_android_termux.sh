#!/data/data/com.termux/files/usr/bin/bash
set -e

REPO_URL="https://github.com/3more102/elsewedy-utilities-application-suite.git"
BRANCH="main"

echo "=============================================="
echo " EUAS v3.9.0 - Android / Termux GitHub Publish"
echo "=============================================="

if [ ! -f "README.md" ] || [ ! -d "app" ]; then
  echo "ERROR: Run this script from inside the EUAS folder."
  exit 1
fi

echo "[1/7] Checking required tools..."
command -v git >/dev/null 2>&1 || pkg install git -y
command -v gh >/dev/null 2>&1 || pkg install gh -y

echo "[2/7] GitHub authentication..."
if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  gh auth login --hostname github.com --git-protocol https --web
fi
gh auth setup-git --hostname github.com

echo "[3/7] Initializing git..."
if [ ! -d ".git" ]; then
  git init
fi
git branch -M "$BRANCH"

git config user.name "omar islam"
git config user.email "197134043+3more102@users.noreply.github.com"

echo "[4/7] Preparing files..."
git add -A

echo "[5/7] Creating commit..."
if ! git diff --cached --quiet; then
  git commit -m "Initial EUAS v3.9.0 release"
else
  echo "No new changes to commit."
fi

echo "[6/7] Configuring remote..."
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REPO_URL"
else
  git remote add origin "$REPO_URL"
fi

echo "[7/7] Pushing to GitHub..."
git push -u origin "$BRANCH"

echo ""
echo "SUCCESS"
echo "Repository:"
echo "https://github.com/3more102/elsewedy-utilities-application-suite"
