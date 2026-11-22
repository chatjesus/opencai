"""
Clerk JWT verification for FastAPI.
Verifies Bearer tokens issued by Clerk using their JWKS endpoint.
Falls back to "anonymous" when no token or invalid token is present.
"""
import logging
import os
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import Request
from jose import jwt, JWTError

logger = logging.getLogger(__name__)

# Simple in-process JWKS cache (keyed by issuer URL)
_jwks_cache: dict[str, dict] = {}


async def _fetch_jwks(jwks_url: str, secret: str = "") -> dict:
    """Fetch JWKS from a server-configured Clerk instance."""
    if jwks_url not in _jwks_cache:
        try:
            headers = {"Authorization": f"Bearer {secret}"} if secret else None
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(jwks_url, headers=headers)
                resp.raise_for_status()
                _jwks_cache[jwks_url] = resp.json()
        except (httpx.HTTPError, ValueError) as err:
            logger.warning("Could not fetch configured Clerk JWKS (%s)", type(err).__name__)
            return {}
    return _jwks_cache.get(jwks_url, {})


async def verify_clerk_token(token: str) -> Optional[str]:
    """
    Verify a Clerk JWT and return the user's Clerk user_id (sub claim).
    Returns None on failure.
    """
    try:
        issuer = os.environ.get("CLERK_JWT_ISSUER", "").strip().rstrip("/")
        if issuer:
            parsed = urlsplit(issuer)
            if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
                return None
            if parsed.username or parsed.password or jwt.get_unverified_claims(token).get("iss") != issuer:
                return None
            jwks = await _fetch_jwks(f"{issuer}/.well-known/jwks.json")
        else:
            secret = os.environ.get("CLERK_SECRET_KEY", "").strip()
            if not secret:
                return None
            jwks = await _fetch_jwks("https://api.clerk.com/v1/jwks", secret)
        if not jwks:
            return None

        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        from jose import jwk as jose_jwk
        key = None
        for k in jwks.get("keys", []):
            if k.get("kid") == kid:
                key = jose_jwk.construct(k)
                break

        if not key:
            logger.debug(f"No matching JWK found for kid={kid}")
            return None

        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer or None,
            options={"verify_aud": False, "require_exp": True},
        )
        authorized_parties = {
            origin.strip() for origin in os.environ.get("CLERK_AUTHORIZED_PARTIES", "").split(",")
            if origin.strip()
        }
        if authorized_parties and claims.get("azp") not in authorized_parties:
            return None
        subject = claims.get("sub")
        return subject if isinstance(subject, str) and subject.startswith("user_") else None

    except JWTError as e:
        logger.debug(f"JWT verification failed: {e}")
        return None
    except Exception as e:
        logger.debug(f"Auth error: {e}")
        return None


async def get_current_user_id(request: Request) -> str:
    """
    FastAPI dependency: extract canonical user_id from Authorization header.
    Supports signed Telegram Mini App session tokens (resolving linked Web
    owners when bound) and Clerk RS256 JWTs.
    Returns the canonical user_id if valid, or "anonymous" as fallback.
    This is intentionally permissive — endpoints choose whether to require auth.
    """
    if hasattr(request.state, "opencai_user_id"):
        return request.state.opencai_user_id
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return "anonymous"

    return await resolve_bearer_user(request, auth[7:].strip())


async def resolve_bearer_user(request: Request, token: str) -> str:
    """Identify the user from a Clerk or opencai Telegram bearer token."""
    if not token:
        return "anonymous"

    # 1. Check signed Telegram Mini App session token first (fast local HS256 check).
    #    Non-webapp tokens immediately return None and fall through to Clerk.
    try:
        from auth.telegram_webapp import is_telegram_webapp_enabled, verify_webapp_session_token
        from models.database import SessionLocal, get_db
        prelim = verify_webapp_session_token(token, db=None) if is_telegram_webapp_enabled() else None
        if prelim is not None:
            override_get_db = getattr(request.app, "dependency_overrides", {}).get(get_db)
            if override_get_db is not None:
                gen = override_get_db()
                db = next(gen)
                try:
                    resolved = verify_webapp_session_token(token, db=db)
                    if resolved and resolved.get("user_id"):
                        return resolved["user_id"]
                finally:
                    try:
                        next(gen)
                    except StopIteration:
                        pass
            else:
                db = SessionLocal()
                try:
                    resolved = verify_webapp_session_token(token, db=db)
                    if resolved and resolved.get("user_id"):
                        return resolved["user_id"]
                finally:
                    db.close()
    except Exception:
        pass

    # 2. Verify Clerk JWT (or test stub in conftest.py).
    user_id = await verify_clerk_token(token)
    return user_id or "anonymous"


def require_auth(user_id: str) -> str:
    """
    Use as a guard in endpoints that need a real user.
    Raises 401 if user is anonymous.
    """
    from fastapi import HTTPException
    if user_id == "anonymous":
        raise HTTPException(status_code=401, detail="Authentication required")
    return user_id
