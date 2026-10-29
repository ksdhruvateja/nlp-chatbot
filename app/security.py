"""Cross-cutting security: signed bearer tokens, property authorization, audit log."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

from fastapi import Depends, Header, HTTPException, Request

from . import db
from .config import get_settings

TOKEN_TTL_S = 60 * 60 * 24 * 7


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_token(user_id: str, ttl_s: int = TOKEN_TTL_S) -> str:
    """Issue an HMAC-signed token. In production the existing FixBridge login
    should mint these (or swap this module for its session/JWT verification)."""
    payload = _b64(json.dumps({"sub": str(user_id), "exp": int(time.time()) + ttl_s}).encode())
    sig = hmac.new(get_settings().secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(sig)}"


def verify_token(token: str) -> str:
    try:
        payload, sig = token.split(".", 1)
        expected = hmac.new(get_settings().secret.encode(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(sig)):
            raise ValueError("bad signature")
        claims = json.loads(_unb64(payload))
        if claims["exp"] < time.time():
            raise ValueError("expired")
        return str(uuid.UUID(claims["sub"]))
    except Exception as exc:  # noqa: BLE001 - any parse failure is an auth failure
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc


def current_user(authorization: str = Header(default="")) -> dict:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    user_id = verify_token(authorization[7:].strip())
    user = db.fetch_one("SELECT id, email, name, role FROM users WHERE id = %s", (user_id,))
    if not user:
        raise HTTPException(status_code=401, detail="Unknown user")
    return user


def require_role(*roles: str):
    def checker(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user
    return checker


def can_access_property(user: dict, property_id: str) -> bool:
    if user["role"] == "admin":
        return True
    row = db.fetch_one(
        """SELECT 1 FROM properties p
           LEFT JOIN property_members m ON m.property_id = p.id AND m.user_id = %(u)s
           WHERE p.id = %(p)s AND (p.owner_id = %(u)s OR m.user_id IS NOT NULL)""",
        {"u": user["id"], "p": property_id},
    )
    return row is not None


def authorize_property(user: dict, property_id: str) -> None:
    try:
        uuid.UUID(str(property_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Property not found") from exc
    if not can_access_property(user, property_id):
        # 404 rather than 403 so property IDs cannot be probed.
        raise HTTPException(status_code=404, detail="Property not found")


def authorize_request(user: dict, request_id: str) -> dict:
    try:
        uuid.UUID(str(request_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Repair request not found") from exc
    req = db.fetch_one("SELECT * FROM repair_requests WHERE id = %s", (request_id,))
    if not req or not can_access_property(user, req["property_id"]):
        raise HTTPException(status_code=404, detail="Repair request not found")
    return req


def audit(actor_id, action: str, target: str | None = None, details: dict | None = None,
          request: Request | None = None) -> None:
    ip = request.client.host if request and request.client else None
    db.execute(
        "INSERT INTO audit_log (actor_id, action, target, details, ip) VALUES (%s, %s, %s, %s, %s)",
        (actor_id, action, target, db.jsonb(details or {}), ip),
    )
