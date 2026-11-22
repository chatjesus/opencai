"""
Telegram Mini App (WebApp) verification, session tokens, allowlist, and
canonical account linkage.

Implements the official Telegram WebApp initData validation protocol:
  1. Parse query string strictly; reject malformed or duplicate fields.
  2. Require valid 64-char hex ``hash``, numeric ``auth_date`` within freshness
     window, and JSON ``user`` object with positive integer ``id``.
  3. Build ``data_check_string`` from sorted ``key=value`` pairs (excluding
     ``hash``) joined with ``\\n``.
  4. ``secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)``
  5. ``expected = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest()``
  6. Compare digests with ``hmac.compare_digest``.

Never logs raw ``initData``, bot tokens, bind codes, or session tokens.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Optional
from urllib.parse import parse_qsl

from jose import JWTError, jwt
from sqlalchemy.orm import Session

from models.database import Character, Conversation, IdentityLink, UserMemory

logger = logging.getLogger(__name__)

WEBAPP_TOKEN_ISSUER = "opencai:webapp"
WEBAPP_TOKEN_TYPE = "webapp_session"
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DIGITS_RE = re.compile(r"^\d+$")
_MAX_INIT_DATA_LEN = 8192


class TelegramAuthError(Exception):
    """Raised when Telegram Mini App verification or access check fails."""

    def __init__(self, detail: str, status_code: int = 401, code: str = "unauthorized"):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class VerifiedTelegramUser:
    telegram_user_id: int
    username: str
    first_name: str
    language_code: str
    auth_date: int


def _truthy(val: str | None, default: bool = False) -> bool:
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def is_telegram_webapp_enabled() -> bool:
    """Opt-in flag for Telegram Mini App authentication."""
    return _truthy(os.environ.get("TELEGRAM_WEBAPP_ENABLED"), default=False)


def get_effective_bot_token() -> str:
    """Resolve Telegram bot token, preferring environment configuration."""
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        from api.settings import _load_config
        cfg = _load_config()
        return str(cfg.get("telegram", {}).get("bot_token", "")).strip()
    except Exception:
        return ""


def get_allowed_telegram_user_ids() -> set[int]:
    """Return allowlisted Telegram numeric user IDs from env and saved config."""
    allowed: set[int] = set()
    raw_env = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if raw_env:
        for part in raw_env.split(","):
            part = part.strip()
            if part and _DIGITS_RE.fullmatch(part):
                allowed.add(int(part))
    try:
        from api.settings import _load_config
        cfg = _load_config()
        for uid in cfg.get("telegram", {}).get("allowed_user_ids", []) or []:
            if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
                allowed.add(uid)
            elif isinstance(uid, str) and _DIGITS_RE.fullmatch(uid.strip()):
                allowed.add(int(uid.strip()))
    except Exception:
        pass
    return allowed


def is_allowlist_enforced_for_webapp() -> bool:
    """Private beta default: require allowlist unless explicitly disabled."""
    raw = os.environ.get("TELEGRAM_WEBAPP_ALLOWLIST_REQUIRED")
    if raw is not None and raw.strip() != "":
        return _truthy(raw, default=True)
    if os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip():
        return True
    try:
        from api.settings import _load_config
        if _load_config().get("telegram", {}).get("allowlist_mode"):
            return True
    except Exception:
        pass
    return True


def is_telegram_user_allowed(telegram_user_id: int, *, webapp: bool = True) -> bool:
    """Check whether a Telegram user passes the private tester allowlist."""
    allowed = get_allowed_telegram_user_ids()
    if webapp:
        if not is_allowlist_enforced_for_webapp():
            return True
        return telegram_user_id in allowed

    # Bot DM mode: enforce when TELEGRAM_ALLOWED_USER_IDS is set or allowlist_mode is on
    env_enforced = _truthy(os.environ.get("TELEGRAM_ALLOWLIST_MODE"), default=False) or bool(
        os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    )
    cfg_enforced = False
    try:
        from api.settings import _load_config
        cfg_enforced = bool(_load_config().get("telegram", {}).get("allowlist_mode", False))
    except Exception:
        pass
    if env_enforced or cfg_enforced:
        return telegram_user_id in allowed
    return True


def _max_auth_age_seconds() -> int:
    raw = os.environ.get("TELEGRAM_WEBAPP_MAX_AGE_SECONDS", "900").strip()
    try:
        val = int(raw)
        return max(30, min(val, 86400))
    except ValueError:
        return 900


def _session_ttl_seconds() -> int:
    raw = os.environ.get("TELEGRAM_SESSION_TTL_SECONDS", "43200").strip()
    try:
        val = int(raw)
        return max(300, min(val, 604800))
    except ValueError:
        return 43200


def _parse_init_data_strict(init_data: str) -> dict[str, str]:
    """Strictly parse initData query string, rejecting malformed or duplicate fields."""
    if not isinstance(init_data, str):
        raise TelegramAuthError("Malformed Telegram initData", 401, "malformed_init_data")
    if not init_data or len(init_data) > _MAX_INIT_DATA_LEN:
        raise TelegramAuthError("Invalid Telegram initData length", 401, "invalid_length")
    if init_data != init_data.strip() or init_data.startswith("?") or "\n" in init_data or "\r" in init_data:
        raise TelegramAuthError("Malformed Telegram initData", 401, "malformed_init_data")

    raw_segments = init_data.split("&")
    if not raw_segments:
        raise TelegramAuthError("Empty Telegram initData", 401, "empty_init_data")
    for seg in raw_segments:
        if not seg or "=" not in seg:
            raise TelegramAuthError("Malformed field in Telegram initData", 401, "malformed_field")
        raw_key, _ = seg.split("=", 1)
        if not raw_key:
            raise TelegramAuthError("Empty field name in Telegram initData", 401, "empty_key")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise TelegramAuthError("Malformed URL encoding in Telegram initData", 401, "bad_encoding")

    if len(pairs) != len(raw_segments):
        raise TelegramAuthError("Malformed Telegram initData segments", 401, "segment_mismatch")

    fields: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in fields:
            raise TelegramAuthError("Duplicate or empty field in Telegram initData", 401, "duplicate_field")
        fields[key] = value
    return fields


def verify_telegram_init_data(
    init_data: str,
    *,
    bot_token: Optional[str] = None,
    now_ts: Optional[int] = None,
    max_age_seconds: Optional[int] = None,
    enforce_allowlist: bool = True,
) -> VerifiedTelegramUser:
    """
    Verify Telegram WebApp ``initData`` per official HMAC-SHA256 specification.
    Raises ``TelegramAuthError`` on any validation, freshness, or allowlist failure.
    """
    resolved_token = (bot_token if bot_token is not None else get_effective_bot_token()).strip()
    if not resolved_token:
        raise TelegramAuthError(
            "Telegram bot token is not configured on the server",
            status_code=503,
            code="bot_unconfigured",
        )

    fields = _parse_init_data_strict(init_data)

    received_hash = fields.get("hash", "")
    if not _HEX64_RE.fullmatch(received_hash):
        raise TelegramAuthError("Missing or malformed Telegram signature hash", 401, "invalid_hash")

    auth_date_raw = fields.get("auth_date", "")
    if not _DIGITS_RE.fullmatch(auth_date_raw):
        raise TelegramAuthError("Missing or malformed Telegram auth_date", 401, "invalid_auth_date")
    auth_date = int(auth_date_raw)

    user_raw = fields.get("user", "")
    if not user_raw:
        raise TelegramAuthError("Missing Telegram user payload", 401, "missing_user")

    # Compute HMAC-SHA256 per Telegram WebApp specification
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(fields.items(), key=lambda kv: kv[0]) if k != "hash"
    )
    secret_key = hmac.new(
        b"WebAppData",
        resolved_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash.lower(), received_hash.lower()):
        raise TelegramAuthError("Telegram initData signature verification failed", 401, "bad_signature")

    # Freshness check AFTER signature check (auth_date is now proven authentic)
    current_ts = int(time.time()) if now_ts is None else int(now_ts)
    max_age = _max_auth_age_seconds() if max_age_seconds is None else int(max_age_seconds)
    if auth_date > current_ts + 60:
        raise TelegramAuthError("Telegram auth_date is in the future", 401, "future_auth_date")
    if current_ts - auth_date > max_age:
        raise TelegramAuthError("Telegram initData has expired; reopen the Mini App", 401, "expired_init_data")

    try:
        user_obj = json.loads(user_raw)
    except (ValueError, TypeError):
        raise TelegramAuthError("Malformed Telegram user JSON", 401, "malformed_user_json")

    if not isinstance(user_obj, dict):
        raise TelegramAuthError("Telegram user field must be a JSON object", 401, "invalid_user_type")

    tg_id = user_obj.get("id")
    if not isinstance(tg_id, int) or isinstance(tg_id, bool) or tg_id <= 0:
        raise TelegramAuthError("Invalid Telegram user ID", 401, "invalid_user_id")

    if enforce_allowlist and not is_telegram_user_allowed(tg_id, webapp=True):
        raise TelegramAuthError(
            "Telegram account is not on the private tester allowlist",
            status_code=403,
            code="not_allowlisted",
        )

    return VerifiedTelegramUser(
        telegram_user_id=tg_id,
        username=str(user_obj.get("username") or ""),
        first_name=str(user_obj.get("first_name") or ""),
        language_code=str(user_obj.get("language_code") or ""),
        auth_date=auth_date,
    )


# ── Session Token Boundary ───────────────────────────────────────────────────

def _get_webapp_signing_key() -> Optional[str]:
    """Resolve signing key for Mini App session tokens without static secrets."""
    explicit = os.environ.get("WEBAPP_SESSION_SECRET", "").strip()
    if explicit:
        return explicit
    bot_token = get_effective_bot_token()
    if bot_token:
        return hmac.new(
            b"opencai-webapp-session-v1",
            bot_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    return None


def create_webapp_session_token(
    canonical_user_id: str,
    telegram_user_id: int | str,
    ttl_seconds: Optional[int] = None,
) -> tuple[str, int]:
    """Mint a signed session JWT for an authenticated Telegram Mini App client."""
    signing_key = _get_webapp_signing_key()
    if not signing_key:
        raise TelegramAuthError(
            "Session signing secret is not configured",
            status_code=503,
            code="session_secret_missing",
        )
    ttl = _session_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
    now_ts = int(time.time())
    claims = {
        "iss": WEBAPP_TOKEN_ISSUER,
        "typ": WEBAPP_TOKEN_TYPE,
        "sub": canonical_user_id,
        "tg_uid": str(telegram_user_id),
        "iat": now_ts,
        "exp": now_ts + ttl,
    }
    token = jwt.encode(claims, signing_key, algorithm="HS256")
    return token, ttl


def verify_webapp_session_token(token: str, db: Optional[Session] = None) -> Optional[dict]:
    """
    Verify an ``opencai:webapp`` session JWT.
    Returns ``{"user_id": canonical_user_id, "tg_uid": tg_uid}`` or ``None`` if
    the token is not a webapp token or fails verification.
    """
    if not token or token.count(".") != 2:
        return None
    try:
        unverified = jwt.get_unverified_claims(token)
    except Exception:
        return None

    if unverified.get("iss") != WEBAPP_TOKEN_ISSUER or unverified.get("typ") != WEBAPP_TOKEN_TYPE:
        return None

    signing_key = _get_webapp_signing_key()
    if not signing_key:
        return None

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["HS256"],
            issuer=WEBAPP_TOKEN_ISSUER,
        )
    except JWTError:
        return None

    sub = str(claims.get("sub") or "").strip()
    tg_uid = str(claims.get("tg_uid") or "").strip()
    if not sub or not _DIGITS_RE.fullmatch(tg_uid):
        return None

    # Enforce live allowlist in case a tester was revoked
    if not is_telegram_user_allowed(int(tg_uid), webapp=True):
        return None

    # Re-resolve canonical linkage so an account linked after token issuance
    # immediately shares the canonical Web owner.
    if db is not None:
        canonical, _ = resolve_telegram_canonical_user(db, tg_uid)
        return {"user_id": canonical, "tg_uid": tg_uid}

    env_map = _parse_env_telegram_links()
    if tg_uid in env_map:
        return {"user_id": env_map[tg_uid], "tg_uid": tg_uid}
    return {"user_id": sub, "tg_uid": tg_uid}


# ── Canonical User Linkage (Web <-> Telegram) ───────────────────────────────

def _parse_env_telegram_links() -> dict[str, str]:
    """
    Parse admin-configured ``TELEGRAM_USER_LINKS`` env mapping.
    Format: ``12345678:user_2abcXYZ,87654321:user_2defUVW``
    """
    raw = os.environ.get("TELEGRAM_USER_LINKS", "").strip()
    mapping: dict[str, str] = {}
    if not raw:
        return mapping
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        tg_part, web_user = item.split(":", 1)
        tg_part = tg_part.strip()
        web_user = web_user.strip()
        if _DIGITS_RE.fullmatch(tg_part) and web_user and not web_user.startswith("tg:"):
            mapping[tg_part] = web_user
    return mapping


def resolve_telegram_canonical_user(db: Session, telegram_user_id: int | str) -> tuple[str, bool]:
    """
    Resolve a verified Telegram user ID to its canonical ``user_id`` string.
    Returns ``(canonical_user_id, is_linked)``.
      - Linked user -> ``("<web_user_id>", True)``
      - Unlinked Telegram user -> ``("tg:<telegram_user_id>", False)``
    """
    tg_str = str(telegram_user_id).strip()
    link = (
        db.query(IdentityLink)
        .filter(
            IdentityLink.platform == "telegram",
            IdentityLink.platform_user_id == tg_str,
            IdentityLink.verified_at != None,  # noqa: E711
            IdentityLink.canonical_user_id != None,  # noqa: E711
        )
        .order_by(IdentityLink.id.desc())
        .first()
    )
    if link and link.canonical_user_id:
        return link.canonical_user_id, True

    env_map = _parse_env_telegram_links()
    if tg_str in env_map:
        return env_map[tg_str], True

    return f"tg:{tg_str}", False


def _migrate_tg_owned_records(db: Session, tg_str: str, canonical_user_id: str) -> None:
    """
    Re-assign conversations, DIY characters, and memories created under ``tg:<id>``
    to ``canonical_user_id`` when a user links their Web and Telegram accounts.
    """
    old_tg_owner = f"tg:{tg_str}"
    if not canonical_user_id or canonical_user_id == old_tg_owner:
        return

    db.query(Conversation).filter(Conversation.clerk_user_id == old_tg_owner).update(
        {Conversation.clerk_user_id: canonical_user_id},
        synchronize_session=False,
    )
    db.query(Character).filter(Character.clerk_creator_id == old_tg_owner).update(
        {Character.clerk_creator_id: canonical_user_id},
        synchronize_session=False,
    )
    # Also migrate memories stored under either "tg:<id>" or raw "<id>" (bot DM)
    for legacy_uid in (old_tg_owner, tg_str):
        mems = db.query(UserMemory).filter(UserMemory.user_id == legacy_uid).all()
        for m in mems:
            clash = (
                db.query(UserMemory)
                .filter(
                    UserMemory.user_id == canonical_user_id,
                    UserMemory.character_id == m.character_id,
                    UserMemory.key == m.key,
                )
                .first()
            )
            if clash:
                db.delete(m)
            else:
                m.user_id = canonical_user_id


def upsert_telegram_identity_link(
    db: Session,
    telegram_user_id: int | str,
    canonical_user_id: str,
) -> IdentityLink:
    """Create or update a verified Telegram -> canonical Web user link and merge records."""
    tg_str = str(telegram_user_id).strip()
    canonical = canonical_user_id.strip()
    if not _DIGITS_RE.fullmatch(tg_str):
        raise ValueError("telegram_user_id must be numeric")
    if not canonical or canonical == "anonymous" or canonical.startswith("tg:"):
        raise ValueError("canonical_user_id must be a real authenticated Web user ID")

    existing = (
        db.query(IdentityLink)
        .filter(
            IdentityLink.platform == "telegram",
            IdentityLink.platform_user_id == tg_str,
        )
        .order_by(IdentityLink.id.desc())
        .first()
    )
    now = datetime.utcnow()
    if existing:
        existing.canonical_user_id = canonical
        existing.verified_at = now
        link = existing
    else:
        link = IdentityLink(
            platform="telegram",
            platform_user_id=tg_str,
            canonical_user_id=canonical,
            verified_at=now,
        )
        db.add(link)

    _migrate_tg_owned_records(db, tg_str, canonical)
    db.commit()
    db.refresh(link)
    return link


def _hash_bind_code(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def create_telegram_bind_code(
    db: Session,
    canonical_user_id: str,
    ttl_seconds: int = 600,
) -> tuple[str, int]:
    """
    Generate a single-use, short-lived bind code for an authenticated Web user.
    Only the SHA-256 digest of the code is stored in ``IdentityLink``.
    """
    canonical = canonical_user_id.strip()
    if not canonical or canonical == "anonymous" or canonical.startswith("tg:"):
        raise ValueError("Must be signed in with a Web account to generate a bind code")

    now = datetime.utcnow()
    # Invalidate any prior unused codes for this user
    prior = (
        db.query(IdentityLink)
        .filter(
            IdentityLink.platform == "telegram_bind",
            IdentityLink.canonical_user_id == canonical,
            IdentityLink.used_at == None,  # noqa: E711
        )
        .all()
    )
    for row in prior:
        row.used_at = now

    raw_code = secrets.token_urlsafe(12)
    code_hash = _hash_bind_code(raw_code)
    pending = IdentityLink(
        platform="telegram_bind",
        platform_user_id=code_hash,
        canonical_user_id=canonical,
        bind_code_hash=code_hash,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    db.add(pending)
    db.commit()
    return raw_code, ttl_seconds


def redeem_telegram_bind_code(
    db: Session,
    telegram_user_id: int | str,
    raw_code: str,
) -> str:
    """
    Redeem a single-use short-lived bind code for ``telegram_user_id``.
    Returns the linked ``canonical_user_id`` or raises ``TelegramAuthError``.
    """
    tg_str = str(telegram_user_id).strip()
    if not _DIGITS_RE.fullmatch(tg_str):
        raise TelegramAuthError("Invalid Telegram user ID", 400, "invalid_tg_id")

    cleaned = (raw_code or "").strip()
    if not cleaned or len(cleaned) > 128 or " " in cleaned:
        raise TelegramAuthError("Invalid or expired bind code", 400, "invalid_bind_code")

    code_hash = _hash_bind_code(cleaned)
    now = datetime.utcnow()

    matched = (
        db.query(IdentityLink)
        .filter(
            IdentityLink.platform == "telegram_bind",
            IdentityLink.bind_code_hash == code_hash,
            IdentityLink.used_at == None,  # noqa: E711
        )
        .first()
    )

    if matched is None:
        raise TelegramAuthError("Invalid or already-used bind code", 400, "invalid_bind_code")

    if not matched.expires_at or matched.expires_at < now or not matched.canonical_user_id:
        matched.used_at = now
        db.commit()
        raise TelegramAuthError("Bind code has expired; generate a new one on Web", 400, "expired_bind_code")

    canonical_user_id = matched.canonical_user_id
    claimed = db.query(IdentityLink).filter(
        IdentityLink.id == matched.id,
        IdentityLink.used_at.is_(None),
    ).update({IdentityLink.used_at: now}, synchronize_session=False)
    if not claimed:
        raise TelegramAuthError("Invalid or already-used bind code", 400, "invalid_bind_code")
    upsert_telegram_identity_link(db, tg_str, canonical_user_id)
    return canonical_user_id
