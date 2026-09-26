from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .maintenance.backup import (
    BackupError,
    BackupRetentionPolicy,
    SNAPSHOT_KINDS,
    apply_backup_retention,
    create_backup,
    create_media_manifest,
    restore_backup,
    verify_backup,
)
from .maintenance.browser_acceptance import BrowserAcceptanceError, run_browser_acceptance
from .core.guards import QueryError
from .torrent.magnet import MagnetError, parse_magnet, parse_magnet_text
from .core.models import SearchBounds
from .search.engine import search
from .api.server import run_server
from .config.settings import load_settings
from .sites.smoke import (
    SMOKE_PUBLIC_FIELDS,
    SmokeCheck,
    run_site_smoke,
    smoke_public_payload,
)
from .sites.smoke_adapters import (
    build_production_smoke_adapters,
    production_smoke_snapshot,
)


_SMOKE_CHILD_MAX_OUTPUT_BYTES = 64 * 1024
_SMOKE_CHILD_TIMEOUT_SECONDS = 12 * 60.0
_SMOKE_CHILD_READ_BYTES = 8 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jav-pilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="bounded metadata search")
    search_parser.add_argument("query")
    search_parser.add_argument(
        "--source", default="all", help="configured source ID, or all"
    )
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--page", type=int, default=1)
    search_parser.add_argument("--pages", type=int, default=1)
    search_parser.add_argument("--detail-limit", type=int, default=5)
    search_parser.add_argument("--no-magnets", action="store_true")
    search_parser.add_argument("--json", action="store_true")

    magnet_parser = subparsers.add_parser("parse-magnet", help="parse magnet URI text")
    magnet_parser.add_argument("text")
    magnet_parser.add_argument("--from-file", action="store_true")
    magnet_parser.add_argument("--json", action="store_true")

    serve_parser = subparsers.add_parser("serve", help="run local HTTP API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8766)

    backup_parser = subparsers.add_parser(
        "backup", help="create, verify, or restore an offline application backup"
    )
    backup_commands = backup_parser.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_commands.add_parser(
        "create", help="create a verified backup"
    )
    backup_create.add_argument("--app-root", type=Path, required=True)
    backup_create.add_argument("--output", type=Path, required=True)
    backup_create.add_argument("--revision", required=True)
    backup_create.add_argument(
        "--kind", choices=tuple(sorted(SNAPSHOT_KINDS)), default="manual"
    )
    backup_create.add_argument("--mark", action="store_true")
    backup_create.add_argument("--label")
    backup_verify = backup_commands.add_parser("verify", help="verify a backup")
    backup_verify.add_argument("backup", type=Path)
    backup_restore = backup_commands.add_parser(
        "restore", help="verify and restore a backup"
    )
    backup_restore.add_argument("backup", type=Path)
    backup_restore.add_argument("--app-root", type=Path, required=True)
    backup_restore.add_argument(
        "--apply",
        action="store_true",
        help="apply after isolated verification; omitted means dry-run",
    )
    backup_prune = backup_commands.add_parser(
        "prune", help="verify snapshots and apply the retention policy"
    )
    backup_prune.add_argument("--backup-root", type=Path, required=True)
    backup_prune.add_argument("--release-state-root", type=Path)
    backup_prune.add_argument("--max-unprotected", type=int, default=10)
    backup_prune.add_argument("--max-age-days", type=int)
    backup_prune.add_argument(
        "--apply",
        action="store_true",
        help="delete the verified plan; omitted means dry-run",
    )
    media_manifest = backup_commands.add_parser(
        "media-manifest", help="create a checksum inventory without copying media"
    )
    media_manifest.add_argument("--media-root", type=Path, required=True)
    media_manifest.add_argument("--output", type=Path, required=True)

    smoke_parser = subparsers.add_parser(
        "smoke-sites",
        help="run bounded, side-effect-free production site probes",
    )
    smoke_parser.add_argument("--format", choices=("json",), default="json")
    smoke_parser.add_argument(
        "--code",
        required=True,
        help="catalog code of a real work to probe (no default is assumed)",
    )
    smoke_parser.add_argument(
        "--sites",
        default="javbus,javdb,jable,supjav,missav",
        help="comma-separated subset of javbus,javdb,jable,supjav,missav",
    )
    smoke_parser.add_argument("--no-retry", action="store_true")

    acceptance_parser = subparsers.add_parser(
        "acceptance-browser",
        help="run a loopback-only independent browser release smoke test",
    )
    acceptance_parser.add_argument("--base-url", default="http://127.0.0.1:8766")

    args = parser.parse_args(argv)

    if args.command == "search":
        return _cmd_search(args)
    if args.command == "parse-magnet":
        return _cmd_parse_magnet(args)
    if args.command == "serve":
        run_server(args.host, args.port)
        return 0
    if args.command == "backup":
        return _cmd_backup(args)
    if args.command == "smoke-sites":
        return _cmd_smoke_sites(args)
    if args.command == "acceptance-browser":
        return _cmd_acceptance_browser(args)

    parser.error("unknown command")
    return 2


def _cmd_search(args: argparse.Namespace) -> int:
    try:
        response = search(
            args.query,
            sources=(args.source,),
            bounds=SearchBounds(
                limit=args.limit,
                page=args.page,
                max_pages=args.pages,
                fetch_magnets=not args.no_magnets,
                detail_limit=args.detail_limit,
            ),
        )
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"query: {response.query}")
    if response.errors:
        for source, error in response.errors.items():
            print(f"{source}: {error}", file=sys.stderr)
    for index, result in enumerate(response.results, start=1):
        magnet_count = len(result.magnets)
        code = f" [{result.code}]" if result.code else ""
        url = f" <{result.url}>" if result.url else ""
        print(f"{index}. {result.title}{code}{url} magnets={magnet_count}")
    return 0 if response.results or not response.errors else 1


def _cmd_parse_magnet(args: argparse.Namespace) -> int:
    text = Path(args.text).read_text(encoding="utf-8") if args.from_file else args.text
    try:
        magnets = parse_magnet_text(text)
        if not magnets:
            magnets = [parse_magnet(text)]
    except MagnetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {"magnets": [magnet.to_dict() for magnet in magnets]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for magnet in magnets:
            print(
                f"info_hash={magnet.info_hash} name={magnet.display_name or ''} trackers={len(magnet.trackers)}"
            )
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    try:
        if args.backup_command == "create":
            path = create_backup(
                args.app_root,
                args.output,
                revision=args.revision,
                snapshot_kind=args.kind,
                user_marked=args.mark,
                label=args.label,
            )
            payload: dict[str, object] = {"ok": True, "backup": str(path)}
        elif args.backup_command == "verify":
            manifest = verify_backup(args.backup)
            payload = {
                "ok": True,
                "backup": str(args.backup),
                "revision": manifest["revision"],
            }
        elif args.backup_command == "restore":
            payload = {
                "ok": True,
                **restore_backup(
                    args.backup,
                    args.app_root,
                    dry_run=not args.apply,
                ),
            }
        elif args.backup_command == "prune":
            payload = {
                "ok": True,
                **apply_backup_retention(
                    args.backup_root,
                    BackupRetentionPolicy(
                        max_unprotected=args.max_unprotected,
                        max_age_days=args.max_age_days,
                    ),
                    dry_run=not args.apply,
                    release_state_root=args.release_state_root,
                ),
            }
        elif args.backup_command == "media-manifest":
            manifest = create_media_manifest(args.media_root, args.output)
            payload = {
                "ok": True,
                "output": str(args.output),
                "file_count": manifest["file_count"],
                "total_bytes": manifest["total_bytes"],
            }
        else:
            raise BackupError("unknown backup command")
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_smoke_sites(args: argparse.Namespace) -> int:
    virtual_display_result = _run_smoke_sites_with_virtual_display(args)
    if virtual_display_result is not None:
        return virtual_display_result
    try:
        sites = tuple(
            site.strip().lower()
            for site in str(args.sites or "").split(",")
            if site.strip()
        )
        adapters = build_production_smoke_adapters(
            load_settings(),
            args.code,
            sites=sites,
            retry_once=not args.no_retry,
        )
        checks = run_site_smoke(adapters, production_smoke_snapshot)
    except Exception:  # noqa: BLE001 - output must never echo upstream details.
        checks = (
            SmokeCheck(
                site="system",
                check="adapter",
                ok=False,
                latency=0,
                error_code="invalid_config",
            ),
        )
    print(
        json.dumps(
            smoke_public_payload(checks),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0 if checks and all(check.ok for check in checks) else 1


def _run_smoke_sites_with_virtual_display(args: argparse.Namespace) -> int | None:
    if not _smoke_sites_require_virtual_display(args):
        return None
    xvfb_run = shutil.which("xvfb-run")
    if xvfb_run is None:
        return _print_smoke_dependency_failure()
    command = [
        xvfb_run,
        "-a",
        sys.executable,
        "-m",
        "jav_pilot.cli",
        "smoke-sites",
        "--format",
        "json",
        "--code",
        str(args.code),
        "--sites",
        str(args.sites),
    ]
    if args.no_retry:
        command.append("--no-retry")
    environment = os.environ.copy()
    environment["JAV_PILOT_SITE_SMOKE_XVFB"] = "1"
    completed = _run_bounded_smoke_child(command, environment)
    if completed is None:
        return _print_smoke_dependency_failure()
    returncode, output = completed
    try:
        payload = json.loads(output.decode("utf-8"))
        public_payload = _validated_smoke_child_payload(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        public_payload = None
    if returncode not in {0, 1} or public_payload is None:
        return _print_smoke_dependency_failure()
    print(json.dumps(public_payload, ensure_ascii=False, separators=(",", ":")))
    return returncode


def _run_bounded_smoke_child(
    command: list[str],
    environment: dict[str, str],
    *,
    timeout_seconds: float = _SMOKE_CHILD_TIMEOUT_SECONDS,
    max_output_bytes: int = _SMOKE_CHILD_MAX_OUTPUT_BYTES,
) -> tuple[int, bytes] | None:
    process: subprocess.Popen[bytes] | None = None
    output = bytearray()
    overflow = threading.Event()
    reader: threading.Thread | None = None
    output_collected = False
    previous_sigterm: object | None = None
    sigterm_handler_installed = False
    try:
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _exit_on_smoke_parent_termination)
            sigterm_handler_installed = True
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
        )
        if process.stdout is None:
            raise OSError("site smoke child stdout is unavailable")
        reader = threading.Thread(
            target=_read_bounded_smoke_output,
            args=(process.stdout, output, overflow, max_output_bytes),
            name="site-smoke-output",
            daemon=True,
        )
        reader.start()
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None or reader.is_alive():
            if overflow.is_set():
                raise ValueError("site smoke child output exceeded its limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            reader.join(min(0.05, remaining))
        if overflow.is_set() or process.returncode is None:
            raise ValueError("site smoke child output is invalid")
        output_collected = True
        return process.returncode, bytes(output)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        if process is not None and not output_collected:
            _kill_smoke_process_group(process)
        if reader is not None:
            reader.join(timeout=5.0)
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if sigterm_handler_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)


def _exit_on_smoke_parent_termination(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def _read_bounded_smoke_output(
    stream: BinaryIO,
    output: bytearray,
    overflow: threading.Event,
    max_output_bytes: int,
) -> None:
    try:
        while chunk := stream.read(_SMOKE_CHILD_READ_BYTES):
            remaining = max_output_bytes - len(output)
            if len(chunk) > remaining:
                output.extend(chunk[: max(0, remaining)])
                overflow.set()
                return
            output.extend(chunk)
    except (OSError, ValueError):
        overflow.set()


def _kill_smoke_process_group(process: subprocess.Popen[bytes]) -> None:
    killed_group = False
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            killed_group = True
        except (OSError, ProcessLookupError):
            pass
    if not killed_group and process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _validated_smoke_child_payload(
    payload: object,
) -> list[dict[str, object]] | None:
    if not isinstance(payload, list) or not payload or len(payload) > 64:
        return None
    checks: list[SmokeCheck] = []
    try:
        for item in payload:
            if not isinstance(item, dict) or not set(item).issubset(
                SMOKE_PUBLIC_FIELDS
            ):
                return None
            values = dict(item)
            if "heights" in values:
                if not isinstance(values["heights"], list):
                    return None
                values["heights"] = tuple(values["heights"])
            checks.append(SmokeCheck(**values))
        return smoke_public_payload(checks)
    except (TypeError, ValueError):
        return None


def _smoke_sites_require_virtual_display(args: argparse.Namespace) -> bool:
    if (
        os.name == "nt"
        or sys.platform == "darwin"
        or os.environ.get("DISPLAY")
        or os.environ.get("JAV_PILOT_SITE_SMOKE_XVFB") == "1"
    ):
        return False
    selected = {
        site.strip().lower()
        for site in str(args.sites or "").split(",")
        if site.strip()
    }
    return not selected or "missav" in selected


def _print_smoke_dependency_failure() -> int:
    checks = (
        SmokeCheck(
            site="system",
            check="adapter",
            ok=False,
            latency=0,
            error_code="dependency_unavailable",
        ),
    )
    print(
        json.dumps(
            smoke_public_payload(checks),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 1


def _cmd_acceptance_browser(args: argparse.Namespace) -> int:
    try:
        payload = run_browser_acceptance(args.base_url)
    except (BrowserAcceptanceError, OSError):
        payload = {"ok": False, "error_code": "browser_acceptance_failed"}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
