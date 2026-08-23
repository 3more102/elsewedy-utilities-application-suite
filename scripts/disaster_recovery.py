"""EUAS disaster-recovery backup, verification, and restore tooling.

Creates self-describing backup directories with SHA-256 manifests. SQLite uses
its online backup API for a consistent database image. PostgreSQL uses pg_dump
custom-format archives and pg_restore for recovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import APP_VERSION, DB_BACKEND, DB_PATH, DATABASE_URL, SCHEMA_VERSION, UPLOAD_DIR

MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_entry(path: Path, root: Path) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _sqlite_quick_check(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {result!r}")
    finally:
        conn.close()


def _backup_sqlite(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {source}")
    source_conn = sqlite3.connect(source)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()
    _sqlite_quick_check(destination)


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f"Required executable not found on PATH: {name}")
    return executable


def _backup_postgres(database_url: str, destination: Path) -> None:
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise ValueError("A PostgreSQL DATABASE_URL is required")
    pg_dump = _require_executable("pg_dump")
    subprocess.run(
        [pg_dump, "--format=custom", "--no-owner", f"--file={destination}", database_url],
        check=True,
    )


def _zip_uploads(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if not source.exists():
            return
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            archive.write(path, path.relative_to(source).as_posix())


def _safe_backup_name(now_utc: datetime) -> str:
    return f"euas-backup-{now_utc.strftime('%Y%m%dT%H%M%SZ')}"


def create_backup(
    output_dir: Path,
    *,
    backend: str = DB_BACKEND,
    sqlite_path: Path = DB_PATH,
    database_url: str = DATABASE_URL,
    uploads_dir: Path = UPLOAD_DIR,
    include_uploads: bool = True,
    now_utc: datetime | None = None,
) -> Path:
    now_utc = now_utc or datetime.now(timezone.utc)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    final_dir = output_dir / _safe_backup_name(now_utc)
    if final_dir.exists():
        raise FileExistsError(f"Backup already exists: {final_dir}")

    staging = Path(tempfile.mkdtemp(prefix=".euas-backup-", dir=output_dir))
    try:
        if backend == "sqlite":
            database_artifact = staging / "database.sqlite3"
            _backup_sqlite(sqlite_path, database_artifact)
        elif backend == "postgresql":
            database_artifact = staging / "database.pgdump"
            _backup_postgres(database_url, database_artifact)
        else:
            raise ValueError(f"Unsupported database backend: {backend}")

        artifacts = [artifact_entry(database_artifact, staging)]
        if include_uploads:
            uploads_artifact = staging / "uploads.zip"
            _zip_uploads(uploads_dir, uploads_artifact)
            artifacts.append(artifact_entry(uploads_artifact, staging))

        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": now_utc.isoformat(),
            "application": "EUAS",
            "app_version": APP_VERSION,
            "schema_version": SCHEMA_VERSION,
            "database_backend": backend,
            "artifacts": artifacts,
        }
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staging.rename(final_dir)
        return final_dir
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_manifest(backup_dir: Path) -> dict:
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeError(f"Missing {MANIFEST_NAME}: {backup_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid backup manifest: {exc}") from exc
    if manifest.get("format_version") != FORMAT_VERSION:
        raise RuntimeError(f"Unsupported backup format_version: {manifest.get('format_version')!r}")
    if manifest.get("database_backend") not in {"sqlite", "postgresql"}:
        raise RuntimeError("Manifest has an unsupported database backend")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeError("Manifest contains no artifacts")
    return manifest


def _validated_artifact_path(backup_dir: Path, relative: str) -> Path:
    candidate = (backup_dir / relative).resolve()
    backup_root = backup_dir.resolve()
    if candidate != backup_root and backup_root not in candidate.parents:
        raise RuntimeError(f"Manifest artifact escapes backup directory: {relative}")
    return candidate


def verify_backup(backup_dir: Path, *, deep: bool = True) -> dict:
    backup_dir = backup_dir.resolve()
    manifest = _load_manifest(backup_dir)
    checked: list[dict] = []
    for item in manifest["artifacts"]:
        relative = item.get("path")
        if not isinstance(relative, str) or not relative:
            raise RuntimeError("Manifest artifact path is invalid")
        path = _validated_artifact_path(backup_dir, relative)
        if not path.is_file():
            raise RuntimeError(f"Backup artifact missing: {relative}")
        actual_size = path.stat().st_size
        actual_hash = sha256_file(path)
        if actual_size != item.get("bytes"):
            raise RuntimeError(f"Backup artifact size mismatch: {relative}")
        if actual_hash != item.get("sha256"):
            raise RuntimeError(f"Backup artifact SHA-256 mismatch: {relative}")
        checked.append({"path": relative, "bytes": actual_size, "sha256": actual_hash})

    if deep and manifest["database_backend"] == "sqlite":
        database_item = next((x for x in checked if x["path"] == "database.sqlite3"), None)
        if not database_item:
            raise RuntimeError("SQLite backup is missing database.sqlite3")
        _sqlite_quick_check(backup_dir / "database.sqlite3")
    elif deep and manifest["database_backend"] == "postgresql":
        database_path = backup_dir / "database.pgdump"
        if not database_path.is_file():
            raise RuntimeError("PostgreSQL backup is missing database.pgdump")
        pg_restore = _require_executable("pg_restore")
        subprocess.run([pg_restore, "--list", str(database_path)], check=True, stdout=subprocess.DEVNULL)

    return {
        "valid": True,
        "backup_dir": str(backup_dir),
        "database_backend": manifest["database_backend"],
        "app_version": manifest.get("app_version"),
        "schema_version": manifest.get("schema_version"),
        "artifacts_checked": len(checked),
    }


def _remove_sqlite_sidecars(target: Path) -> None:
    # EUAS runs SQLite in WAL mode. Replacing only the main database file while
    # a crashed host's -wal/-shm sidecars survive lets the next open replay
    # pre-restore frames over (or corrupt) the restored image.
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = target.with_name(target.name + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _restore_uploads(archive_path: Path, target: Path, *, force: bool) -> None:
    if target.exists() and any(target.iterdir()) and not force:
        raise RuntimeError(f"Uploads target is not empty; use --force: {target}")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            member_path = (target / member.filename).resolve()
            if target.resolve() not in member_path.parents and member_path != target.resolve():
                raise RuntimeError(f"Unsafe uploads archive member: {member.filename}")
        archive.extractall(target)


def restore_backup(
    backup_dir: Path,
    *,
    sqlite_target: Path = DB_PATH,
    target_database_url: str = "",
    uploads_target: Path = UPLOAD_DIR,
    restore_uploads: bool = False,
    force: bool = False,
) -> dict:
    verification = verify_backup(backup_dir, deep=True)
    backup_dir = backup_dir.resolve()
    manifest = _load_manifest(backup_dir)
    backend = manifest["database_backend"]

    if backend == "sqlite":
        source = backup_dir / "database.sqlite3"
        sqlite_target = sqlite_target.resolve()
        sqlite_target.parent.mkdir(parents=True, exist_ok=True)
        if sqlite_target.exists() and not force:
            raise RuntimeError(f"SQLite target exists; use --force: {sqlite_target}")
        temp_target = sqlite_target.with_name(sqlite_target.name + ".restore-tmp")
        try:
            shutil.copy2(source, temp_target)
            _sqlite_quick_check(temp_target)
        except Exception:
            temp_target.unlink(missing_ok=True)
            raise
        _remove_sqlite_sidecars(sqlite_target)
        os.replace(temp_target, sqlite_target)
    else:
        if not target_database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("--target-database-url is required for PostgreSQL restore")
        # pg_restore --clean drops and recreates every object in the target
        # database, so a mistyped URL silently destroys a live deployment.
        # Require the same explicit --force gate as the SQLite path.
        if not force:
            raise RuntimeError(
                "PostgreSQL restore replaces existing objects (--clean); use --force"
            )
        pg_restore = _require_executable("pg_restore")
        subprocess.run(
            [
                pg_restore,
                "--clean",
                "--if-exists",
                "--no-owner",
                f"--dbname={target_database_url}",
                str(backup_dir / "database.pgdump"),
            ],
            check=True,
        )

    uploads_archive = backup_dir / "uploads.zip"
    if restore_uploads and uploads_archive.is_file():
        _restore_uploads(uploads_archive, uploads_target.resolve(), force=force)

    return {
        **verification,
        "restored": True,
        "uploads_restored": bool(restore_uploads and uploads_archive.is_file()),
    }


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EUAS disaster-recovery tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="Create a verified backup package")
    backup.add_argument("--output-dir", type=Path, default=ROOT / "backups")
    backup.add_argument("--no-uploads", action="store_true")

    verify = sub.add_parser("verify", help="Verify a backup package")
    verify.add_argument("backup_dir", type=Path)
    verify.add_argument("--shallow", action="store_true", help="Skip DB-native integrity validation")

    restore = sub.add_parser("restore", help="Restore a verified backup package")
    restore.add_argument("backup_dir", type=Path)
    restore.add_argument("--sqlite-target", type=Path, default=DB_PATH)
    restore.add_argument("--target-database-url", default="")
    restore.add_argument("--restore-uploads", action="store_true")
    restore.add_argument("--uploads-target", type=Path, default=UPLOAD_DIR)
    restore.add_argument("--force", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            path = create_backup(args.output_dir, include_uploads=not args.no_uploads)
            result = verify_backup(path, deep=True)
            _print_json({**result, "created": True})
        elif args.command == "verify":
            _print_json(verify_backup(args.backup_dir, deep=not args.shallow))
        else:
            _print_json(
                restore_backup(
                    args.backup_dir,
                    sqlite_target=args.sqlite_target,
                    target_database_url=args.target_database_url,
                    uploads_target=args.uploads_target,
                    restore_uploads=args.restore_uploads,
                    force=args.force,
                )
            )
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
