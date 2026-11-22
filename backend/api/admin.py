"""
Admin API — read/update the configurable adult-operations layer and explicit
Telegram <-> Web account mappings.

Gate:
  - For ops-config: if env OPS_ADMIN_TOKEN is set, requests must send a matching
    `X-Admin-Token` header (constant-time compared). If unset (local dev), open.
  - For identity-link management: `OPS_ADMIN_TOKEN` is ALWAYS required so public
    callers can never inspect or reassign user identities.
"""
import hmac
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.telegram_webapp import upsert_telegram_identity_link
from auth.private_beta import private_beta_enabled
from models.database import IdentityLink, get_db
from services.ops_config import get_ops_config, set_ops_values

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(request: Request, *, strict: bool = False) -> None:
    token = os.getenv("OPS_ADMIN_TOKEN", "").strip()
    if not token:
        if strict or private_beta_enabled():
            raise HTTPException(
                status_code=403,
                detail="OPS_ADMIN_TOKEN must be configured for identity administration",
            )
        return  # dev: no gate configured for ops-config
    provided = request.headers.get("X-Admin-Token", "")
    if not provided or not hmac.compare_digest(provided.encode("utf-8"), token.encode("utf-8")):
        raise HTTPException(status_code=403, detail="admin only")


class TelegramIdentityLinkUpsert(BaseModel):
    telegram_user_id: str = Field(..., min_length=1, max_length=64)
    canonical_user_id: str = Field(..., min_length=1, max_length=200)


@router.get("/ops-config")
def read_ops_config(request: Request, db: Session = Depends(get_db)) -> dict:
    """Return the full operations config (defaults overlaid with overrides)."""
    _require_admin(request)
    return get_ops_config(db)


@router.put("/ops-config")
async def update_ops_config(request: Request, db: Session = Depends(get_db)) -> dict:
    """Update one or more levers. Body is a flat {key: value} object."""
    _require_admin(request)
    updates = await request.json()
    if not isinstance(updates, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return set_ops_values(db, updates)


@router.get("/telegram-links")
def list_telegram_links(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    """List verified Telegram <-> Web user links (strictly admin-only)."""
    _require_admin(request, strict=True)
    rows = (
        db.query(IdentityLink)
        .filter(
            IdentityLink.platform == "telegram",
            IdentityLink.verified_at != None,  # noqa: E711
        )
        .order_by(IdentityLink.id.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "telegram_user_id": r.platform_user_id,
            "canonical_user_id": r.canonical_user_id,
            "verified_at": r.verified_at.isoformat() if r.verified_at else None,
        }
        for r in rows
    ]


@router.put("/telegram-links")
def upsert_telegram_link(
    payload: TelegramIdentityLinkUpsert,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Admin-managed explicit mapping between a Telegram user ID and Web user ID."""
    _require_admin(request, strict=True)
    try:
        link = upsert_telegram_identity_link(
            db,
            telegram_user_id=payload.telegram_user_id,
            canonical_user_id=payload.canonical_user_id,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {
        "id": link.id,
        "telegram_user_id": link.platform_user_id,
        "canonical_user_id": link.canonical_user_id,
        "verified_at": link.verified_at.isoformat() if link.verified_at else None,
    }
