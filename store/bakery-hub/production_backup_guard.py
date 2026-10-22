"""Local identity and archive checks for an approved PostgreSQL backup.

No credentials are accepted as arguments. libpq service and pass files are supplied
by the operator outside the repository. Restore requires an explicit subcommand.
"""

import argparse
import configparser
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo


SERVICE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}\Z")
DATABASE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}\Z")
ENCRYPTED_NAME = re.compile(r"bakeryhub_\d{8}_\d{6}_[0-9a-f]{12}\.dump\.age\Z")
BACKUP_DIR = Path("/home/tosane/BakeryHub-Backup/database")


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


def source_preflight(service, database):
    if not DATABASE.fullmatch(database):
        raise ValueError("Invalid expected database name")
    source = identity(service)
    if source[0] != database or source[3] != 18 or client_major("pg_dump") != 18:
        raise ValueError("Source identity or PostgreSQL 18 pg_dump does not match")
    # Detect missing SELECT grants before creating any output. pg_dump itself
    # remains the final authority, including sequences and non-public schemas.
    access = run("psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                 "--dbname", f"service={service}", "--command",
                 "SELECT count(*), bool_and(has_schema_privilege(n.oid, 'USAGE') "
                 "AND has_table_privilege(c.oid, 'SELECT')) "
                 "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                 "WHERE n.nspname='public' AND c.relkind IN ('r','p','m','f')")
    match = re.fullmatch(r"(\d+)\|(t|f)", access)
    if not match or int(match[1]) == 0 or match[2] != "t":
        raise ValueError("Source tables are missing or cannot all be read")
    return source, int(match[1])


def private_file(path):
    file = Path(path)
    details = file.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError("Expected an owned, private regular file (0600), not a symlink")
    return file


def private_directory(path):
    directory = Path(path)
    details = directory.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise ValueError("Backup directory must be owned and mode 0700")
    return directory


def check_local_configuration():
    for name in ("PGSERVICEFILE", "PGPASSFILE"):
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"{name} must refer to a protected local file")
        private_file(value)
    if os.environ.get("PGPASSWORD"):
        raise ValueError("Do not pass a password via PGPASSWORD")


def backup_directory():
    # No user-controlled destination argument: backup bytes never enter a repo
    # or the Work filesystem when this command is run on the G580.
    parent = private_directory(BACKUP_DIR.parent)
    directory = private_directory(parent / "database")
    if directory.resolve() != BACKUP_DIR.resolve():
        raise ValueError("Backup location changed")
    return directory


def recipient_file(path):
    file = private_file(path)
    recipients = [line.strip() for line in file.read_text(encoding="ascii").splitlines()]
    if len(recipients) != 1 or not re.fullmatch(r"age1[a-z0-9]{20,}", recipients[0]):
        raise ValueError("Exactly one age public recipient is required")
    if file.parent.resolve() == BACKUP_DIR.resolve():
        raise ValueError("Recipient configuration belongs outside the backup directory")
    return file


def pipe_commands(producer_command, consumer_command, *, output=subprocess.DEVNULL,
                  capture=False, allow_producer_early_exit=False):
    producer = subprocess.Popen(producer_command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        consumer = subprocess.Popen(consumer_command, stdin=producer.stdout,
                                    stdout=subprocess.PIPE if capture else output,
                                    stderr=subprocess.DEVNULL)
        producer.stdout.close()
        result, _ = consumer.communicate()
        producer_code = producer.wait()
        if consumer.returncode != 0 or (producer_code != 0 and not allow_producer_early_exit):
            raise ValueError("A streaming command failed; output is incomplete")
        return result
    finally:
        if producer.poll() is None:
            producer.terminate()
            producer.wait()


def encrypted_backup(source_service, source_database, confirm_source,
                     web_db_match_confirmed, recipient):
    if confirm_source != source_database or not web_db_match_confirmed:
        raise ValueError("Source and Web database match must be confirmed separately")
    check_local_configuration()
    source_preflight(source_service, source_database)
    folder = backup_directory()
    recipient = recipient_file(recipient)
    timestamp = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d_%H%M%S")
    filename = f"bakeryhub_{timestamp}_{uuid.uuid4().hex[:12]}.dump.age"
    final = folder / filename
    partial = folder / (filename + ".partial")
    original_umask = os.umask(0o077)
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as sink:
                pipe_commands(["pg_dump", "--dbname", f"service={source_service}",
                               "--format=custom", "--no-owner", "--no-acl"],
                              ["age", "--encrypt", "--recipients-file", str(recipient)],
                              output=sink)
                sink.flush()
                os.fsync(sink.fileno())
            if partial.stat().st_size == 0:
                raise ValueError("Encrypted output is empty")
            # Hard linking is atomic and fails instead of overwriting a final file.
            os.link(partial, final, follow_symlinks=False)
            partial.unlink()
            folder_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(folder_fd)
            finally:
                os.close(folder_fd)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    finally:
        os.umask(original_umask)
    size, digest = encrypted_file_digest(final)
    return final, size, digest


def encrypted_file_digest(path):
    archive = private_file(path)
    if archive.parent.resolve() != backup_directory().resolve() or not ENCRYPTED_NAME.fullmatch(archive.name):
        raise ValueError("Not a finalized backup in the fixed G580 directory")
    size = archive.stat().st_size
    if size == 0:
        raise ValueError("Encrypted backup is empty")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return size, digest


def verify_encrypted_archive(path, identity_file, expected_sha256):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Expected SHA-256 must be a lowercase hex digest")
    identity_file = private_file(identity_file)
    if identity_file.parent.resolve() == BACKUP_DIR.resolve():
        raise ValueError("Decryption key must not be stored with the backup")
    size, digest = encrypted_file_digest(path)
    if digest != expected_sha256:
        raise ValueError("Encrypted file SHA-256 mismatch")
    decrypt = ["age", "--decrypt", "--identity", str(identity_file), str(path)]
    # The SQL stream is discarded in /dev/null. No plain dump is written.
    pipe_commands(decrypt, ["pg_restore", "--file", os.devnull])
    # pg_restore --list may stop after the TOC. The first pass already checked
    # the entire authenticated age stream, so an early SIGPIPE here is normal.
    listing = pipe_commands(decrypt, ["pg_restore", "--list"], capture=True,
                            allow_producer_early_exit=True).decode("utf-8", errors="replace")
    for entry in (" TABLE public datasets ", " TABLE public products ",
                  " TABLE public daily_sales ", " TABLE public alembic_version ",
                  " TABLE DATA public daily_sales "):
        if entry not in listing:
            raise ValueError("Encrypted archive lacks a required table or data entry")
    return size, digest


def restore_encrypted_approved(args):
    if args.confirm_target != args.target_database:
        raise ValueError("Explicit restore target confirmation does not match")
    check_local_configuration()
    preflight(args.source_service, args.source_database,
              args.target_service, args.target_database)
    if client_major("pg_restore") != 18:
        raise ValueError("PostgreSQL 18 pg_restore is required")
    size, _ = verify_encrypted_archive(args.path, args.identity_file, args.sha256)
    # Recheck emptiness immediately before writing, after the full archive pass.
    preflight(args.source_service, args.source_database,
              args.target_service, args.target_database)
    pipe_commands(["age", "--decrypt", "--identity", args.identity_file, args.path],
                  ["pg_restore", "--single-transaction", "--exit-on-error",
                   "--no-owner", "--no-acl", "--dbname", f"service={args.target_service}"])
    return size


def offline_approval(path):
    """Read a separately controlled, manually approved snapshot record."""
    record_file = private_file(path)
    private_directory(record_file.parent)
    if record_file.parent.resolve() == backup_directory().resolve():
        raise ValueError("Approval record cannot be stored with the encrypted archive")
    if record_file.stat().st_size > 4096:
        raise ValueError("Approval record exceeds size limit")
    record = json.loads(record_file.read_text(encoding="utf-8"))
    fields = {"backup_id", "captured_at", "source_database", "source_service",
              "source_host", "source_address", "source_port", "source_server_id",
              "postgres_major", "encrypted_size", "encrypted_sha256",
              "target_service", "target_database", "target_host", "target_address",
              "target_port", "target_server_id", "approved_at", "approval_reference"}
    if not isinstance(record, dict) or record.keys() != fields:
        raise ValueError("Incomplete offline approval record")
    if any(type(record[key]) is not str for key in (
            "backup_id", "captured_at", "source_database", "source_service", "source_host",
            "source_address", "source_server_id", "encrypted_sha256", "target_service",
            "target_database", "target_host", "target_address", "target_server_id",
            "approved_at", "approval_reference")):
        raise ValueError("Invalid offline approval field type")
    if not ENCRYPTED_NAME.fullmatch(record["backup_id"]):
        raise ValueError("Invalid approved backup ID")
    if not all(DATABASE.fullmatch(record[key]) for key in ("source_database", "target_database")):
        raise ValueError("Invalid approved database name")
    if not all(SERVICE.fullmatch(record[key]) for key in ("source_service", "target_service")):
        raise ValueError("Invalid approved service name")
    if any(not isinstance(record[key], str) or not re.fullmatch(r"[a-zA-Z0-9._-]{3,253}", record[key])
           for key in ("source_host", "target_host", "source_server_id", "target_server_id")):
        raise ValueError("Invalid approved server identifier")
    for key in ("source_address", "target_address"):
        ipaddress.ip_address(record[key])
    if any(type(record[key]) is not int or not 1 <= record[key] <= 65535
           for key in ("source_port", "target_port")):
        raise ValueError("Invalid approved port")
    if record["postgres_major"] != 18 or type(record["encrypted_size"]) is not int or record["encrypted_size"] <= 0:
        raise ValueError("Invalid approved version or size")
    if not isinstance(record["encrypted_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["encrypted_sha256"]):
        raise ValueError("Invalid approved SHA-256")
    for key in ("captured_at", "approved_at"):
        value = datetime.fromisoformat(record[key])
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Approval times must include timezone")
    if datetime.fromisoformat(record["approved_at"]) < datetime.fromisoformat(record["captured_at"]):
        raise ValueError("Approval predates capture")
    if not isinstance(record["approval_reference"], str) or not re.fullmatch(r"[a-zA-Z0-9._/-]{4,128}", record["approval_reference"]):
        raise ValueError("Independent approval reference required")
    return record


def offline_target_preflight(record):
    """Fail closed when service configuration, live server, or isolation is unclear."""
    if record["source_service"] == record["target_service"] or record["source_server_id"] == record["target_server_id"]:
        raise ValueError("Source and target identifiers overlap")
    if (record["source_address"], record["source_port"]) == (record["target_address"], record["target_port"]):
        raise ValueError("Source and target addresses overlap")
    if record["source_host"] == record["target_host"] and record["source_port"] == record["target_port"]:
        raise ValueError("Source and target host overlap")
    if client_major("pg_restore") != 18 or client_major("psql") != 18:
        raise ValueError("PostgreSQL 18 clients are required")
    for name in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGSERVICE", "PGOPTIONS", "PGSSLMODE"):
        if os.environ.get(name):
            raise ValueError("Connection override is forbidden for offline restore")
    settings = configparser.ConfigParser(interpolation=None)
    settings.read(os.environ["PGSERVICEFILE"])
    service = record["target_service"]
    if service not in settings:
        raise ValueError("Approved target service is missing")
    section = settings[service]
    if (section.get("host"), section.get("port"), section.get("dbname")) != (
            record["target_host"], str(record["target_port"]), record["target_database"]):
        raise ValueError("Target service configuration differs from independent approval")
    if any(key in section for key in ("hostaddr", "service", "options")):
        raise ValueError("Target service contains an identity override")
    target = identity(service)
    if target != (record["target_database"], record["target_address"], record["target_port"], 18):
        raise ValueError("Live restore target does not match approval")
    if (target[1], target[2]) == (record["source_address"], record["source_port"]):
        raise ValueError("Restore target resolves to recorded source")
    present = run("psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "--dbname", f"service={service}",
                  "--command", "SELECT count(*) FROM pg_namespace WHERE nspname NOT IN "
                  "('public','information_schema') AND nspname NOT LIKE 'pg_%'")
    relations = run("psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "--dbname", f"service={service}",
                    "--command", "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')")
    if present != "0" or relations != "0":
        raise ValueError("Offline restore target is not empty")
    state = run("psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "--dbname", f"service={service}",
                "--command", "SELECT (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()), "
                "has_schema_privilege(current_user, 'public', 'CREATE'), "
                "(SELECT pg_get_userbyid(datdba)=current_user FROM pg_database WHERE datname=current_database())")
    if state != "1|t|t":
        raise ValueError("Target has competing sessions or insufficient restore ownership")
    return target


def restore_encrypted_offline_approved(args):
    """Restore an approved encrypted snapshot without contacting its source."""
    check_local_configuration()
    record = offline_approval(args.approval_record)
    if (args.confirm_source, args.confirm_target, args.confirm_backup_id) != (
            record["source_database"], record["target_database"], record["backup_id"]):
        raise ValueError("Explicit offline confirmations differ from approval")
    if Path(args.path).name != record["backup_id"]:
        raise ValueError("Encrypted backup ID differs from approval")
    offline_target_preflight(record)
    size, digest = verify_encrypted_archive(args.path, args.identity_file, record["encrypted_sha256"])
    if (size, digest) != (record["encrypted_size"], record["encrypted_sha256"]):
        raise ValueError("Encrypted backup differs from approval")
    offline_target_preflight(record)
    pipe_commands(["age", "--decrypt", "--identity", args.identity_file, args.path],
                  ["pg_restore", "--single-transaction", "--exit-on-error", "--no-owner", "--no-acl",
                   "--dbname", f"service={record['target_service']}"])
    return size


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
    encrypted = sub.add_parser("backup-encrypted")
    encrypted.add_argument("--source-service", required=True)
    encrypted.add_argument("--source-database", required=True)
    encrypted.add_argument("--confirm-source", required=True)
    encrypted.add_argument("--web-db-match-confirmed", action="store_true")
    encrypted.add_argument("--recipient-file", required=True)
    verify = sub.add_parser("verify-encrypted")
    verify.add_argument("--identity-file", required=True)
    verify.add_argument("--sha256", required=True)
    verify.add_argument("path")
    encrypted_restore = sub.add_parser("restore-encrypted-approved")
    for name in ("source-service", "source-database", "target-service", "target-database"):
        encrypted_restore.add_argument(f"--{name}", required=True)
    encrypted_restore.add_argument("--identity-file", required=True)
    encrypted_restore.add_argument("--confirm-target", required=True)
    encrypted_restore.add_argument("--sha256", required=True)
    encrypted_restore.add_argument("path")
    offline = sub.add_parser("restore-encrypted-offline-approved")
    offline.add_argument("--approval-record", required=True)
    offline.add_argument("--identity-file", required=True)
    offline.add_argument("--confirm-source", required=True)
    offline.add_argument("--confirm-target", required=True)
    offline.add_argument("--confirm-backup-id", required=True)
    offline.add_argument("path")
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
        elif args.action == "backup-encrypted":
            path, size, digest = encrypted_backup(
                args.source_service, args.source_database, args.confirm_source,
                args.web_db_match_confirmed, args.recipient_file)
            print(f"ENCRYPTED: {path}; size={size} bytes; SHA-256={digest}; "
                  "decryption and restore NOT TESTED")
        elif args.action == "verify-encrypted":
            size, digest = verify_encrypted_archive(args.path, args.identity_file, args.sha256)
            print(f"PASS: decrypted custom archive and complete stream; size={size}; "
                  f"encrypted SHA-256={digest}; restore NOT TESTED")
        elif args.action == "restore-encrypted-approved":
            size = restore_encrypted_approved(args)
            print(f"RESTORED to confirmed isolated target; encrypted size={size} bytes; "
                  "row and constraint comparison still required")
        elif args.action == "restore-encrypted-offline-approved":
            size = restore_encrypted_offline_approved(args)
            print(f"RESTORED to independently approved isolated target; encrypted size={size} bytes; "
                  "row and constraint comparison still required")
        else:
            size = restore_approved(args)
            print(f"RESTORED to confirmed isolated target; archive size={size} bytes; "
                  "row and constraint comparison still required")
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        # Never emit subprocess stderr: libpq errors can contain connection details.
        parser.exit(1, f"FAIL: {type(exc).__name__}; inspect protected local logs\n")


if __name__ == "__main__":
    main()
