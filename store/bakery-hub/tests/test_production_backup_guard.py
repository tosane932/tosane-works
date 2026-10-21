"""No external server or real archive is touched by these guard tests."""

import os
from pathlib import Path
import subprocess
from argparse import Namespace

import pytest

import production_backup_guard as guard


def test_preflight_rejects_wrong_source_database(monkeypatch):
    monkeypatch.setattr(guard, "identity", lambda service: {
        "source": ("unexpected", "192.0.2.1", 5432, 18),
        "target": ("isolated", "127.0.0.1", 5432, 18)}[service])
    with pytest.raises(ValueError, match="Database name"):
        guard.preflight("source", "bakery_db_4", "target", "isolated")


@pytest.mark.parametrize("identities,reason", [
    ({"source": ("bakery_db_4", "192.0.2.1", 5432, 18),
      "target": ("isolated", "192.0.2.1", 5432, 18)}, "same PostgreSQL server"),
    ({"source": ("bakery_db_4", "192.0.2.1", 5432, 18),
      "target": ("isolated", "127.0.0.1", 5432, 16)}, "PostgreSQL 18"),
])
def test_preflight_rejects_same_server_or_version(monkeypatch, identities, reason):
    monkeypatch.setattr(guard, "identity", identities.__getitem__)
    with pytest.raises(ValueError, match=reason):
        guard.preflight("source", "bakery_db_4", "target", "isolated")


def test_preflight_rejects_old_client_and_nonempty_target(monkeypatch):
    monkeypatch.setattr(guard, "identity", lambda service: (
        ("bakery_db_4", "192.0.2.1", 5432, 18) if service == "source"
        else ("isolated", "127.0.0.1", 5432, 18)))
    monkeypatch.setattr(guard, "client_major", lambda tool: 16)
    with pytest.raises(ValueError, match="clients"):
        guard.preflight("source", "bakery_db_4", "target", "isolated")
    monkeypatch.setattr(guard, "client_major", lambda tool: 18)
    monkeypatch.setattr(guard, "run", lambda *args: "1" if "pg_class" in args[-1] else "0")
    with pytest.raises(ValueError, match="not empty"):
        guard.preflight("source", "bakery_db_4", "target", "isolated")


def test_preflight_accepts_independent_empty_pg18_servers(monkeypatch):
    monkeypatch.setattr(guard, "identity", lambda service: (
        ("bakery_db_4", "192.0.2.1", 5432, 18) if service == "source"
        else ("isolated", "127.0.0.1", 5432, 18)))
    monkeypatch.setattr(guard, "client_major", lambda tool: 18)
    monkeypatch.setattr(guard, "run", lambda *args: "0")
    assert guard.preflight("source", "bakery_db_4", "target", "isolated")[1][0] == "isolated"


def test_verify_archive_rejects_missing_empty_permissions_and_corruption(monkeypatch, tmp_path):
    path = tmp_path / "backup.dump"
    with pytest.raises(ValueError, match="Missing"):
        guard.verify_archive(path)
    path.write_bytes(b"anything")
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="permissions"):
        guard.verify_archive(path)
    os.chmod(path, 0o600)
    monkeypatch.setattr(guard, "run", lambda *args: "1; TABLE public products\n2; TABLE DATA public products")
    def broken_restore(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr=b"corrupt")
    monkeypatch.setattr(guard.subprocess, "run", broken_restore)
    with pytest.raises(subprocess.CalledProcessError):
        guard.verify_archive(path)


def test_verify_archive_hashes_complete_stream(monkeypatch, tmp_path):
    path = tmp_path / "backup.dump"
    path.write_bytes(b"synthetic")
    os.chmod(path, 0o600)
    monkeypatch.setattr(guard, "run", lambda *args: "1; TABLE public products\n2; TABLE DATA public products")
    monkeypatch.setattr(guard.subprocess, "run", lambda *args, **kwargs: None)
    size, digest = guard.verify_archive(path)
    assert size == 9
    assert digest == "b3cc0475bb78a5026098858e9889acf666d31062d513d303314eca31d36e72f2"


def test_restore_refuses_mismatched_confirmation_or_hash(monkeypatch):
    args = Namespace(source_service="source", source_database="bakery_db_4",
                     target_service="target", target_database="isolated",
                     confirm_target="wrong", sha256="a" * 64, path="/unused")
    with pytest.raises(ValueError, match="confirmation"):
        guard.restore_approved(args)
    args.confirm_target = "isolated"
    monkeypatch.setattr(guard, "preflight", lambda *a: None)
    monkeypatch.setattr(guard, "verify_archive", lambda *a: (9, "b" * 64))
    with pytest.raises(ValueError, match="SHA-256"):
        guard.restore_approved(args)


def test_source_preflight_does_not_require_restore_target(monkeypatch):
    monkeypatch.setattr(guard, "identity", lambda service: ("bakery_db_4", "192.0.2.1", 5432, 18))
    monkeypatch.setattr(guard, "client_major", lambda tool: 18)
    monkeypatch.setattr(guard, "run", lambda *args: "27|t")
    assert guard.source_preflight("source", "bakery_db_4")[1] == 27
    monkeypatch.setattr(guard, "run", lambda *args: "27|f")
    with pytest.raises(ValueError, match="cannot all be read"):
        guard.source_preflight("source", "bakery_db_4")


def test_encrypted_backup_requires_manual_match_and_never_finalizes_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "BACKUP_DIR", tmp_path / "BakeryHub-Backup" / "database")
    guard.BACKUP_DIR.mkdir(parents=True, mode=0o700)
    os.chmod(guard.BACKUP_DIR.parent, 0o700)
    recipient = tmp_path / "recipient.txt"
    recipient.write_text("age1" + "a" * 30 + "\n")
    os.chmod(recipient, 0o600)
    monkeypatch.setattr(guard, "check_local_configuration", lambda: None)
    monkeypatch.setattr(guard, "source_preflight", lambda *args: None)
    with pytest.raises(ValueError, match="confirmed"):
        guard.encrypted_backup("source", "bakery_db_4", "bakery_db_4", False, recipient)
    def failed_stream(*args, **kwargs):
        kwargs["output"].write(b"partial encrypted bytes")
        raise ValueError("pg_dump failed")
    monkeypatch.setattr(guard, "pipe_commands", failed_stream)
    with pytest.raises(ValueError, match="pg_dump failed"):
        guard.encrypted_backup("source", "bakery_db_4", "bakery_db_4", True, recipient)
    assert list(guard.BACKUP_DIR.iterdir()) == []


def test_encrypted_restore_refuses_wrong_target_before_decrypt(monkeypatch):
    args = Namespace(source_service="source", source_database="bakery_db_4",
                     target_service="target", target_database="isolated",
                     confirm_target="production", sha256="a" * 64,
                     identity_file="/unused", path="/unused")
    monkeypatch.setattr(guard, "check_local_configuration", lambda: pytest.fail("too early"))
    with pytest.raises(ValueError, match="confirmation"):
        guard.restore_encrypted_approved(args)
