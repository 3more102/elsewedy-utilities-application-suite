# Publish EUAS v3.9.0 to GitHub from Android

Target repository:

`3more102/elsewedy-utilities-application-suite`

## 1. Install Termux

Install Termux from F-Droid or the official Termux GitHub releases.

## 2. Download this ZIP from ChatGPT

Save it in your Android **Download** folder, then extract it with Samsung My Files / Files.

You should get a folder named:

`EUAS`

## 3. Open Termux and run

```bash
pkg update -y
pkg install git gh -y
termux-setup-storage
```

When Android asks for file access, tap **Allow**.

## 4. Go to the extracted project

Usually:

```bash
cd /storage/emulated/0/Download/EUAS
```

If you extracted it inside another folder, adjust the path.

Check:

```bash
ls
```

You should see `README.md`, `app`, `static`, `tests`, etc.

## 5. Publish

```bash
chmod +x publish_from_android_termux.sh
./publish_from_android_termux.sh
```

The script will open GitHub authentication in your browser if needed.

Approve the login, return to Termux, and let it finish.

## 6. Verify

Open:

`https://github.com/3more102/elsewedy-utilities-application-suite`

You should see the full EUAS source code, README, tests, documentation, Docker files and GitHub Actions workflow.

## If `cd` says the folder does not exist

Run:

```bash
find /storage/emulated/0/Download -maxdepth 3 -type f -name README.md 2>/dev/null
```

Then use the parent folder of the EUAS README.
