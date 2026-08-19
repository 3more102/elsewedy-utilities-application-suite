# Publish EUAS v3.9.0 to GitHub

Target repository:

`https://github.com/3more102/elsewedy-utilities-application-suite`

## Windows — easiest method

1. Install **Git for Windows** if it is not already installed.
2. Extract this ZIP.
3. Open the `EUAS` folder.
4. Double-click `publish_to_github_windows.bat`.
5. If GitHub opens a browser authentication window, sign in as `3more102` and authorize Git Credential Manager.
6. Wait for the script to print `SUCCESS`.

The script initializes Git locally, commits the complete EUAS v3.9.0 source tree, sets the remote repository, and pushes branch `main`.

## PowerShell alternative

```powershell
powershell -ExecutionPolicy Bypass -File .\publish_to_github.ps1
```

## Linux/macOS

```bash
chmod +x publish_to_github_linux_mac.sh
./publish_to_github_linux_mac.sh
```

## Why this file exists

The normal GitHub app in ChatGPT provides read/search access. Direct repository pushes from ChatGPT require a write-capable Codex/GitHub environment. These scripts publish the same GitHub-ready EUAS tree directly from your machine.
