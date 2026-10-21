"""Local identity and archive checks for an approved PostgreSQL backup.

No credentials are accepted as arguments. libpq service and pass files are supplied
by the operator outside the repository. Restore requires an explicit subcommand.
"""

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess


SERVICE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}\Z")
DATABASE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}\Z")


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def client_major(tool):
    match = re.search(r"\b(\d+)\.\d+\b", run(tool, "--version"))
    if not match:
        raise ValueError(f"{tool}: client version cannot be determined")
    return int(match.group(1))


def identity(service):
    if not SERVICE.fullmatch(service):
        raise ValueError("Invalid libpq service name")
    # No URL, password, or row contents are printed. TCP address is mandatory.
    row = run("psql", "-X", "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1",
              "--dbname", f"service={service}", "--command",
              "SELECT current_database(), inet_server_addr(), inet_server_port(), "
              "current_setting('server_version_num')").split("|")
    if len(row) != 4 or not row[1] or not row[2].isdigit() or not row[3].isdigit():
        raise ValueError("Cannot establish TCP database identity")
    return row[0], row[1], int(row[2]), int(row[3]) // 10000


def preflight(source_service, source_database, target_service, target_database):
    if not DATABASE.fullmatch(source_database) or not DATABASE.fullmatch(target_database):
        raise ValueError("Invalid expected database name")
    source = identity(source_service)
    target = identity(target_service)
    if source[0] != source_database or target[0] != target_database:
        raise ValueError("Database name does not match the independently verified name")
    if source[1:3] == target[1:3]:
        raise ValueError("Source and target resolve to the same PostgreSQL server")
    if source[3] != 18 or target[3] != 18:
        raise ValueError("Both servers must be PostgreSQL 18 for this procedure")
    if client_major("pg_dump") != 18 or client_major("pg_restore") != 18:
        raise ValueError("PostgreSQL 18 dump and restore clients are required")
    # Empty means no user schemas or relations, not merely zero business rows.
    present = run("psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                  "--dbname", f"service={target_service}", "--command",
                  "SELECT count(*) FROM pg_namespace WHERE nspname NOT IN "
                  "('public', 'information_schema') AND nspname NOT LIKE 'pg_%'")
    relations = run("psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                    "--dbname", f"service={target_service}", "--command",
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S','f')")
    if present != "0" or relations != "0":
        raise ValueError("Restore target is not empty")
    return source, target


def verify_archive(path):
    archive = Path(path)
    if not archive.is_file() or archive.is_symlink() or archive.stat().st_size == 0:
        raise ValueError("Missing, empty, or symbolic-link archive")
    if archive.stat().st_mode & 0o077:
        raise ValueError("Archive permissions are wider than 0600")
    listing = run("pg_restore", "--list", str(archive))
    if " TABLE public " not in listing or " TABLE DATA public " not in listing:
        raise ValueError("Archive lacks public table schema or table data entries")
    # --list alone does not read the compressed table data. Drain the whole SQL
    # stream to a null sink to catch truncation/corruption without writing a DB.
    with open(os.devnull, "w") as sink:
        subprocess.run(["pg_restore", "--file", os.devnull, str(archive)],
                       check=True, stdout=sink, stderr=subprocess.PIPE)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return archive.stat().st_size, digest


def restore_approved(args):
    if args.confirm_target != args.target_database:
        raise ValueError("Explicit restore target confirmation does not match")
    if not re.fullmatch(r"[0-9a-f]{64}", args.sha256):
        raise ValueError("Expected SHA-256 must be a lowercase hex digest")
    preflight(args.source_service, args.source_database,
              args.target_service, args.target_database)
    size, digest = verify_archive(args.path)
    if digest != args.sha256:
        raise ValueError("Archive SHA-256 does not match approved digest")
    # Never use --clean or --create. Single transaction prevents partial restore
    # on error. The destination is checked again immediately before this call.
    subprocess.run(["pg_restore", "--single-transaction", "--exit-on-error",
                    "--no-owner", "--no-acl", "--dbname",
                    f"service={args.target_service}", args.path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("preflight")
    check.add_argument("--source-service", required=True)
    check.add_argument("--source-database", required=True)
    check.add_argument("--target-service", required=True)
    check.add_argument("--target-database", required=True)
    archive = sub.add_parser("verify-archive")
    archive.add_argument("path")
    restore = sub.add_parser("restore-approved")
    for name in ("source-service", "source-database", "target-service", "target-database"):
        restore.add_argument(f"--{name}", required=True)
    restore.add_argument("--confirm-target", required=True)
    restore.add_argument("--sha256", required=True)
    restore.add_argument("path")
    args = parser.parse_args()
    try:
        if args.action == "preflight":
            source, target = preflight(args.source_service, args.source_database,
                                       args.target_service, args.target_database)
            print(f"PASS: source={source[0]} PostgreSQL {source[3]}; "
                  f"empty isolated target={target[0]} PostgreSQL {target[3]}")
        elif args.action == "verify-archive":
            size, digest = verify_archive(args.path)
            print(f"PASS: size={size} bytes; SHA-256={digest}")
        else:
            size = restore_approved(args)
            print(f"RESTORED to confirmed isolated target; archive size={size} bytes; "
                  "row and constraint comparison still required")
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        # Never emit subprocess stderr: libpq errors can contain connection details.
        parser.exit(1, f"FAIL: {type(exc).__name__}; inspect protected local logs\n")


if __name__ == "__main__":
    main()
