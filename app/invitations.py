"""Private-demo invitation and account lifecycle operations.

These functions are used by the local operator CLI only. No public admin route
is registered by the application.
"""

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .db import connect
from .security import env_flag


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invitations_required() -> bool:
    production_default = os.getenv("APP_ENV", "development").strip().lower() == "production"
    return env_flag("DEMO_INVITATIONS_REQUIRED", production_default)


def create_invitation(expires_in_days: int = 7) -> tuple[str, str]:
    if not 1 <= expires_in_days <= 90:
        raise ValueError("Invitation expiry must be between 1 and 90 days.")
    invitation_id = uuid.uuid4().hex
    token = f"ma1.{invitation_id}.{secrets.token_urlsafe(32)}"
    now = _now()
    with connect() as db:
        db.execute(
            "INSERT INTO invitations(id,token_hash,created_at,expires_at) VALUES(?,?,?,?)",
            (invitation_id, _token_hash(token), now.isoformat(),
             (now + timedelta(days=expires_in_days)).isoformat()),
        )
    return invitation_id, token


def revoke_invitation(invitation_id: str) -> bool:
    with connect() as db:
        cursor = db.execute(
            "UPDATE invitations SET revoked_at=? WHERE id=? AND redeemed_at IS NULL AND revoked_at IS NULL",
            (_now().isoformat(), invitation_id),
        )
        return cursor.rowcount == 1


def disable_account(username: str) -> bool:
    with connect() as db:
        user = db.execute("SELECT id FROM users WHERE username=?", (username.strip().lower(),)).fetchone()
        if not user:
            return False
        db.execute("UPDATE users SET disabled_at=? WHERE id=?", (_now().isoformat(), user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        return True


def enable_account(username: str) -> bool:
    with connect() as db:
        cursor = db.execute(
            "UPDATE users SET disabled_at=NULL WHERE username=? AND disabled_at IS NOT NULL",
            (username.strip().lower(),),
        )
        return cursor.rowcount == 1


def consume_invitation(db, token: str | None) -> None:
    """Validate and atomically redeem a code inside the caller's transaction."""
    if not token or len(token) > 160:
        raise HTTPException(400, "A valid, unused demo invitation code is required.")
    parts = token.split(".", 2)
    if len(parts) != 3 or parts[0] != "ma1" or len(parts[1]) != 32:
        raise HTTPException(400, "A valid, unused demo invitation code is required.")
    row = db.execute(
        "SELECT id,token_hash,expires_at,redeemed_at,revoked_at FROM invitations WHERE id=?",
        (parts[1],),
    ).fetchone()
    if not row or not secrets.compare_digest(row["token_hash"], _token_hash(token)):
        raise HTTPException(400, "A valid, unused demo invitation code is required.")
    if row["redeemed_at"] or row["revoked_at"] or row["expires_at"] <= _now().isoformat():
        raise HTTPException(400, "A valid, unused demo invitation code is required.")
    cursor = db.execute(
        "UPDATE invitations SET redeemed_at=? WHERE id=? AND redeemed_at IS NULL AND revoked_at IS NULL AND expires_at>?",
        (_now().isoformat(), row["id"], _now().isoformat()),
    )
    if cursor.rowcount != 1:
        raise HTTPException(400, "A valid, unused demo invitation code is required.")
