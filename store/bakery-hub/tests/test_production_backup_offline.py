"""Synthetic offline approval/target refusal tests; no external DB access."""

from argparse import Namespace
import json
import os

import pytest

import production_backup_guard as guard


def approved():
    return dict(backup_id="bakeryhub_20261021_214150_271fcbe5bdc0.dump.age",
                captured_at="2026-10-21T21:41:50+09:00", source_database="synthetic_source",
                source_service="ci_source", source_host="source.example.test",
                source_address="192.0.2.10", source_port=5432, source_server_id="synthetic-source-id",
                postgres_major=18, encrypted_size=123, encrypted_sha256="a" * 64,
                target_service="ci_target", target_database="isolated_restore",
                target_host="127.0.0.1", target_address="127.0.0.1", target_port=5433,
                target_server_id="synthetic-target-id", approved_at="2026-10-22T09:00:00+09:00",
                approval_reference="local-approved-ticket-1")


def fixture_approval(monkeypatch, tmp_path, record=None):
    folder = tmp_path / "BakeryHub-Backup" / "database"
    folder.mkdir(parents=True, mode=0o700)
    folder.parent.chmod(0o700)
    monkeypatch.setattr(guard, "BACKUP_DIR", folder)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir(mode=0o700)
    approval_file = approval_dir / "approved.json"
    approval_file.write_text(json.dumps(record if record is not None else approved()))
    approval_file.chmod(0o600)
    service = tmp_path / "pg_service.conf"
    service.write_text("[ci_target]\nhost=127.0.0.1\nport=5433\ndbname=isolated_restore\nuser=ci_test_user\n")
    service.chmod(0o600)
    password = tmp_path / "pgpass"
    password.write_text("synthetic-placeholder\n")
    password.chmod(0o600)
    monkeypatch.setenv("PGSERVICEFILE", str(service))
    monkeypatch.setenv("PGPASSFILE", str(password))
    monkeypatch.delenv("PGPASSWORD", raising=False)
    return approval_file


def test_approval_rejects_incomplete_private_or_collocated_record(monkeypatch, tmp_path):
    path = fixture_approval(monkeypatch, tmp_path)
    assert guard.offline_approval(path) == approved()
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        guard.offline_approval(path)
    path.chmod(0o600)
    path.write_text("{}")
    with pytest.raises(ValueError, match="Incomplete"):
        guard.offline_approval(path)
    path.write_text(json.dumps(approved()))
    collocated = guard.BACKUP_DIR / "approved.json"
    collocated.write_text(path.read_text())
    collocated.chmod(0o600)
    with pytest.raises(ValueError, match="cannot be stored"):
        guard.offline_approval(collocated)


@pytest.mark.parametrize("change,reason", [
    ({"encrypted_sha256": "g" * 64}, "SHA-256"),
    ({"encrypted_size": 0}, "size"),
    ({"postgres_major": 16}, "version"),
    ({"source_service": "ci_target"}, "overlap"),
    ({"target_server_id": "synthetic-source-id"}, "overlap"),
    ({"target_address": "192.0.2.10", "target_port": 5432}, "overlap"),
])
def test_offline_refuses_bad_record_or_source_alias(monkeypatch, tmp_path, change, reason):
    record = approved() | change
    path = fixture_approval(monkeypatch, tmp_path, record)
    monkeypatch.setattr(guard, "client_major", lambda tool: 18)
    with pytest.raises(ValueError, match=reason):
        guard.offline_target_preflight(guard.offline_approval(path)) if reason == "overlap" else guard.offline_approval(path)


def test_offline_target_rejects_alias_nonempty_sessions_and_old_server(monkeypatch, tmp_path):
    path = fixture_approval(monkeypatch, tmp_path)
    record = guard.offline_approval(path)
    monkeypatch.setattr(guard, "client_major", lambda tool: 18)
    calls = []
    def live_identity(service):
        calls.append(service)
        return "isolated_restore", "127.0.0.1", 5433, 18
    monkeypatch.setattr(guard, "identity", live_identity)
    monkeypatch.setattr(guard, "run", lambda *args: "1|t|t" if "pg_stat_activity" in args[-1] else "0")
    assert guard.offline_target_preflight(record)[0] == "isolated_restore"
    assert calls == ["ci_target"]
    monkeypatch.setattr(guard, "run", lambda *args: "2|t|t" if "pg_stat_activity" in args[-1] else "0")
    with pytest.raises(ValueError, match="competing"):
        guard.offline_target_preflight(record)
    monkeypatch.setattr(guard, "run", lambda *args: "1" if "pg_class" in args[-1] else "0")
    with pytest.raises(ValueError, match="not empty"):
        guard.offline_target_preflight(record)
    monkeypatch.setattr(guard, "identity", lambda service: ("isolated_restore", "127.0.0.1", 5433, 16))
    with pytest.raises(ValueError, match="Live restore target"):
        guard.offline_target_preflight(record)
    monkeypatch.setattr(guard, "identity", lambda service: pytest.fail("service alias must be rejected first"))
    service = tmp_path / "pg_service.conf"
    service.write_text("[ci_target]\nhost=source.example.test\nport=5432\ndbname=synthetic_source\n")
    with pytest.raises(ValueError, match="configuration differs"):
        guard.offline_target_preflight(record)


def test_offline_rechecks_before_restore_and_never_calls_source(monkeypatch, tmp_path):
    path = fixture_approval(monkeypatch, tmp_path)
    record = approved()
    calls = []
    monkeypatch.setattr(guard, "offline_target_preflight", lambda value: calls.append("target"))
    monkeypatch.setattr(guard, "verify_encrypted_archive", lambda *a: (123, "a" * 64))
    monkeypatch.setattr(guard, "pipe_commands", lambda *a, **kw: calls.append("restore"))
    monkeypatch.setattr(guard, "preflight", lambda *a: pytest.fail("source queried"))
    monkeypatch.setattr(guard, "source_preflight", lambda *a: pytest.fail("source queried"))
    args = Namespace(approval_record=str(path), identity_file=str(tmp_path / "identity"),
                     confirm_source=record["source_database"], confirm_target=record["target_database"],
                     confirm_backup_id=record["backup_id"], path=str(guard.BACKUP_DIR / record["backup_id"]))
    assert guard.restore_encrypted_offline_approved(args) == 123
    assert calls == ["target", "target", "restore"]
    args.confirm_backup_id = "different"
    with pytest.raises(ValueError, match="confirmations"):
        guard.restore_encrypted_offline_approved(args)


def test_offline_does_not_restore_after_changed_target_or_archive(monkeypatch, tmp_path):
    path = fixture_approval(monkeypatch, tmp_path)
    record = approved()
    args = Namespace(approval_record=str(path), identity_file="identity", confirm_source=record["source_database"],
                     confirm_target=record["target_database"], confirm_backup_id=record["backup_id"],
                     path=str(guard.BACKUP_DIR / record["backup_id"]))
    writes = []
    monkeypatch.setattr(guard, "pipe_commands", lambda *a, **kw: writes.append(True))
    monkeypatch.setattr(guard, "offline_target_preflight", lambda record: None)
    monkeypatch.setattr(guard, "verify_encrypted_archive", lambda *a: (122, "a" * 64))
    with pytest.raises(ValueError, match="differs"):
        guard.restore_encrypted_offline_approved(args)
    assert not writes
    monkeypatch.setattr(guard, "verify_encrypted_archive", lambda *a: (123, "a" * 64))
    attempts = iter((None, ValueError("target changed")))
    def changed_target(record):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
    monkeypatch.setattr(guard, "offline_target_preflight", changed_target)
    with pytest.raises(ValueError, match="target changed"):
        guard.restore_encrypted_offline_approved(args)
    assert not writes
