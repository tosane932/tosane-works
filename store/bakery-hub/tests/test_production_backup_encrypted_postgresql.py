"""Streaming age backup and restore between two synthetic PostgreSQL 18 servers.

Runs only when the dedicated CI supplies both disposable endpoints. No Render
credentials, production data, plaintext dump file, or GitHub artifact is used.
"""

from argparse import Namespace
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import psycopg2
import pytest

import production_backup_guard as guard
from test_production_backup_offline import approved


pytestmark = pytest.mark.skipif(
    os.environ.get("ENCRYPTED_BACKUP_SYNTHETIC_TEST") != "1",
    reason="two disposable PostgreSQL 18 CI servers are required",
)


def _connection(port, database):
    return psycopg2.connect(host="127.0.0.1", port=port, dbname=database,
                            user="ci_test_user", password="ci_test_password")


def _snapshot(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
        names = [row[0] for row in cursor.fetchall()]
        counts = {}
        for name in names:
            cursor.execute(f'SELECT count(*) FROM "{name}"')
            counts[name] = cursor.fetchone()[0]
        cursor.execute("SELECT sum(s.quantity*p.price) FROM daily_sales s "
                       "JOIN products p ON p.id=s.product_id")
        revenue = cursor.fetchone()[0]
        cursor.execute("SELECT encode(sha256(photo), 'hex') FROM products ORDER BY id")
        images = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT version_num FROM alembic_version")
        version = cursor.fetchone()[0]
        cursor.execute("SELECT conname, contype FROM pg_constraint c "
                       "JOIN pg_namespace n ON n.oid=c.connamespace "
                       "WHERE n.nspname='public' ORDER BY 1")
        constraints = cursor.fetchall()
        cursor.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public'")
        sequences = cursor.fetchall()
    connection.rollback()
    return names, counts, revenue, images, version, constraints, sequences


def test_encrypted_stream_backup_verify_and_isolated_restore(monkeypatch, tmp_path):
    for tool in ("psql", "pg_dump", "pg_restore", "age", "age-keygen"):
        assert shutil.which(tool), f"{tool} required for synthetic integration"
    assert guard.client_major("pg_dump") == guard.client_major("pg_restore") == 18
    source_port, target_port = 5432, 5433
    with _connection(source_port, "sales_data_app_test") as source:
        with source.cursor() as cursor:
            cursor.execute("CREATE TABLE datasets (id integer PRIMARY KEY, system_key text UNIQUE)")
            cursor.execute("CREATE TABLE products (id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                           "dataset_id integer REFERENCES datasets(id), name text NOT NULL, "
                           "price integer CHECK (price > 0), photo bytea)")
            cursor.execute("CREATE TABLE daily_sales (id integer PRIMARY KEY, "
                           "product_id integer REFERENCES products(id), quantity integer CHECK (quantity > 0))")
            cursor.execute("CREATE TABLE alembic_version (version_num text PRIMARY KEY)")
            cursor.execute("INSERT INTO datasets VALUES (1, 'admin')")
            cursor.execute("INSERT INTO products(dataset_id,name,price,photo) "
                           "VALUES (1,'synthetic bread',280,decode('aabbcc','hex'))")
            cursor.execute("INSERT INTO daily_sales VALUES (1,1,3)")
            cursor.execute("INSERT INTO alembic_version VALUES ('synthetic-only')")
    with _connection(source_port, "sales_data_app_test") as source:
        expected = _snapshot(source)

    service = tmp_path / "pg_service.conf"
    service.write_text("[ci_backup_source]\nhost=127.0.0.1\nport=5432\n"
                       "dbname=sales_data_app_test\nuser=ci_test_user\nsslmode=disable\n"
                       "[ci_backup_target]\nhost=127.0.0.1\nport=5433\n"
                       "dbname=restore_test\nuser=ci_test_user\nsslmode=disable\n")
    password = tmp_path / "pgpass"
    password.write_text("127.0.0.1:5432:sales_data_app_test:ci_test_user:ci_test_password\n"
                        "127.0.0.1:5433:restore_test:ci_test_user:ci_test_password\n")
    for path in (service, password):
        path.chmod(0o600)
    monkeypatch.setenv("PGSERVICEFILE", str(service))
    monkeypatch.setenv("PGPASSFILE", str(password))
    monkeypatch.delenv("PGPASSWORD", raising=False)

    backup_dir = tmp_path / "BakeryHub-Backup" / "database"
    backup_dir.mkdir(parents=True, mode=0o700)
    backup_dir.parent.chmod(0o700)
    monkeypatch.setattr(guard, "BACKUP_DIR", backup_dir)
    identity = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    identity.chmod(0o600)
    recipient = tmp_path / "recipient.txt"
    with recipient.open("wb") as sink:
        subprocess.run(["age-keygen", "-y", str(identity)], check=True,
                       stdout=sink, stderr=subprocess.DEVNULL)
    recipient.chmod(0o600)

    # Backup must work while there is no restore target configured for it.
    path, size, digest = guard.encrypted_backup(
        "ci_backup_source", "sales_data_app_test", "sales_data_app_test", True, recipient)
    assert path.parent == backup_dir and path.stat().st_mode & 0o077 == 0
    assert size > 0 and hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert path.read_bytes().startswith(b"age-encryption.org/v1")
    assert not list(tmp_path.rglob("*.dump"))

    assert not list(backup_dir.glob("*.partial"))
    assert guard.verify_encrypted_archive(path, identity, digest) == (size, digest)

    corrupted = backup_dir / "bakeryhub_20261021_000000_123456abcdef.dump.age"
    corrupted.write_bytes(path.read_bytes()[:-30])
    corrupted.chmod(0o600)
    damaged_digest = hashlib.sha256(corrupted.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="streaming command failed"):
        guard.verify_encrypted_archive(corrupted, identity, damaged_digest)

    args = Namespace(source_service="ci_backup_source", source_database="sales_data_app_test",
                     target_service="ci_backup_target", target_database="restore_test",
                     confirm_target="restore_test", identity_file=str(identity),
                     sha256=digest, path=str(path))
    guard.restore_encrypted_approved(args)
    with _connection(target_port, "restore_test") as target:
        assert _snapshot(target) == expected
        with target.cursor() as cursor:
            cursor.execute("SAVEPOINT invalid_fk")
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cursor.execute("INSERT INTO daily_sales VALUES (2,999,1)")
            cursor.execute("ROLLBACK TO SAVEPOINT invalid_fk")
        target.rollback()
    assert not list(tmp_path.rglob("*.dump"))

    # A second empty database on the independent target server exercises the
    # offline path while the source service has been removed entirely.
    admin = _connection(target_port, "restore_test")
    try:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute("CREATE DATABASE offline_test")
    finally:
        admin.close()
    service.write_text("[ci_offline_target]\nhost=127.0.0.1\nport=5433\n"
                       "dbname=offline_test\nuser=ci_test_user\nsslmode=disable\n")
    password.write_text("127.0.0.1:5433:offline_test:ci_test_user:ci_test_password\n")
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir(mode=0o700)
    record = approved() | dict(
        backup_id=path.name, source_database="sales_data_app_test", source_service="ci_backup_source",
        source_host="127.0.0.1", source_address="127.0.0.1", source_port=5432,
        target_service="ci_offline_target", target_database="offline_test",
        target_host="127.0.0.1", target_address="127.0.0.1", target_port=5433,
        encrypted_size=size, encrypted_sha256=digest,
    )
    approval = approval_dir / "approved.json"
    approval.write_text(json.dumps(record))
    approval.chmod(0o600)
    offline_args = Namespace(approval_record=str(approval), identity_file=str(identity),
                             confirm_source="sales_data_app_test", confirm_target="offline_test",
                             confirm_backup_id=path.name, path=str(path))
    with pytest.raises(ValueError, match="SHA-256"):
        record["encrypted_sha256"] = "b" * 64
        approval.write_text(json.dumps(record))
        guard.restore_encrypted_offline_approved(offline_args)
    record["encrypted_sha256"] = digest
    approval.write_text(json.dumps(record))
    wrong_key = tmp_path / "wrong-identity.txt"
    subprocess.run(["age-keygen", "-o", str(wrong_key)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wrong_key.chmod(0o600)
    offline_args.identity_file = str(wrong_key)
    with pytest.raises(ValueError, match="streaming command failed"):
        guard.restore_encrypted_offline_approved(offline_args)
    offline_args.identity_file = str(identity)
    damaged = backup_dir / "bakeryhub_20261021_000000_123456abcdef.dump.age"
    record["backup_id"] = damaged.name
    record["encrypted_size"] = damaged.stat().st_size
    record["encrypted_sha256"] = damaged_digest
    approval.write_text(json.dumps(record))
    offline_args.confirm_backup_id = damaged.name
    offline_args.path = str(damaged)
    with pytest.raises(ValueError, match="streaming command failed"):
        guard.restore_encrypted_offline_approved(offline_args)
    record["backup_id"] = path.name
    record["encrypted_size"] = size
    record["encrypted_sha256"] = digest
    approval.write_text(json.dumps(record))
    offline_args.confirm_backup_id = path.name
    offline_args.path = str(path)
    assert guard.restore_encrypted_offline_approved(offline_args) == size
    with _connection(target_port, "offline_test") as offline_target:
        assert _snapshot(offline_target) == expected
    with pytest.raises(ValueError, match="not empty"):
        guard.restore_encrypted_offline_approved(offline_args)
    assert not list(tmp_path.rglob("*.dump"))
