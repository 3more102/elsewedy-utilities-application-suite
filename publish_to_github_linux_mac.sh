#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
REPO="https://github.com/3more102/elsewedy-utilities-application-suite.git"

command -v git >/dev/null 2>&1 || { echo "Git is required."; exit 1; }
[ -d .git ] || git init
git branch -M main

git config user.name >/dev/null 2>&1 || git config user.name "EUAS Developers"
git config user.email >/dev/null 2>&1 || git config user.email "euas@local.invalid"

git add -A
if ! git diff --cached --quiet; then
  git commit -m "Initial EUAS v3.9.0 release"
fi

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REPO"
else
  git remote add origin "$REPO"
fi

git push -u origin main
echo "Published: https://github.com/3more102/elsewedy-utilities-application-suite"
