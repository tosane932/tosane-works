"""Admin login records; callers own the transaction and never persist raw tokens."""

import datetime
import hashlib
import re
import secrets

from flask import current_app
from sqlalchemy import case, select, update

from models import AdminAuthSession, db, utc_now


ADMIN_SESSION_TOKEN_KEY = "admin_session_token"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def token_hash(token):
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def active_admin_session(token):
    digest = token_hash(token)
    if digest is None:
        return False
    now = utc_now()
    # Always issue a SELECT: identity-map or process caches must not hide logout.
    return db.session.execute(select(AdminAuthSession.token_hash).where(
        AdminAuthSession.token_hash == digest,
        AdminAuthSession.revoked_at.is_(None),
        AdminAuthSession.issued_at <= now,
        AdminAuthSession.expires_at > now,
    )).first() is not None


def revoke_admin_session(token):
    digest = token_hash(token)
    if digest is None:
        return
    now = utc_now()
    db.session.execute(update(AdminAuthSession).where(
        AdminAuthSession.token_hash == digest,
        AdminAuthSession.revoked_at.is_(None),
    ).values(revoked_at=case(
        (AdminAuthSession.issued_at > now, AdminAuthSession.issued_at), else_=now,
    )))


def issue_admin_session(previous_token=None):
    lifetime = current_app.config["ADMIN_SESSION_MAX_AGE_SECONDS"]
    if type(lifetime) is not int or lifetime <= 0:
        raise ValueError("Invalid Admin session lifetime configuration")
    now = utc_now()
    token = secrets.token_urlsafe(32)
    revoke_admin_session(previous_token)
    db.session.add(AdminAuthSession(
        token_hash=token_hash(token), issued_at=now,
        expires_at=now + datetime.timedelta(seconds=lifetime),
    ))
    db.session.flush()
    return token
