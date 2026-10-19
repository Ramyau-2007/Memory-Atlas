import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Request
from .db import connect
from .security import env_flag
from .invitations import consume_invitation, invitations_required

def _password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return salt.hex() + ":" + digest.hex()

def _valid(password: str, encoded: str) -> bool:
    salt, expected = encoded.split(":", 1)
    return hmac.compare_digest(_password(password, bytes.fromhex(salt)).split(":", 1)[1], expected)

def register(username: str, password: str, db_path: str | None = None,
             invitation_code: str | None = None) -> int:
    username = username.strip().lower()
    if len(username) < 3 or len(username) > 64 or not username.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "Username must be 3–64 letters, numbers, underscores, or hyphens.")
    if len(password) < 10:
        raise HTTPException(400, "Password must contain at least 10 characters.")
    require_invitation = invitations_required()
    password_hash = _password(password)
    try:
        with connect(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            if require_invitation:
                consume_invitation(db, invitation_code)
            cur = db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", (username, password_hash))
            return int(cur.lastrowid)
    except HTTPException:
        raise
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "That username is already registered.") from exc
        raise

def login(username: str, password: str, db_path: str | None = None) -> str:
    with connect(db_path) as db:
        user = db.execute("SELECT * FROM users WHERE username=?", (username.strip().lower(),)).fetchone()
        if not user or not _valid(password, user["password_hash"]):
            raise HTTPException(401, "Incorrect username or password.")
        if user["disabled_at"] is not None:
            raise HTTPException(403, "This account is disabled. Contact the demo organizer.")
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user["id"], expires))
        return token

def current_user(request: Request) -> int:
    token = request.headers.get("authorization", "")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    elif env_flag("SESSION_COOKIE_AUTH"):
        token = request.cookies.get("session", "")
    else:
        token = ""
    if not token:
        raise HTTPException(401, "Sign in to continue.")
    with connect() as db:
        row = db.execute("""
            SELECT s.user_id,s.expires_at FROM sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND u.disabled_at IS NULL
        """, (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(401, "Session expired. Please sign in again.")
    return int(row["user_id"])

def revoke(token: str) -> None:
    if token:
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?",(hashlib.sha256(token.encode()).hexdigest(),))
