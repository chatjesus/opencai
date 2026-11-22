"""Access boundary for the hosted, invite-only app."""
import os
from posixpath import normpath

from fastapi import Request
from sqlalchemy import select
from starlette.responses import JSONResponse

from auth.clerk import get_current_user_id
from auth.telegram_webapp import is_telegram_webapp_enabled, verify_webapp_session_token
from models.database import Conversation, GeneratedIllustration, SessionLocal


def private_beta_enabled() -> bool:
    return (
        os.getenv("OPENCAI_PRIVATE_BETA", "").strip().lower() in {"1", "true", "yes"}
        or os.getenv("LLM_PROVIDER", "").strip().lower() == "xai"
        or os.getenv("IMAGE_PROVIDER", "").strip().lower() == "xai"
    )


def _allowlist(name: str) -> set[str]:
    return {part.strip() for part in os.getenv(name, "").split(",") if part.strip()}


async def _beta_member(request: Request, user_id: str) -> bool:
    if user_id in _allowlist("OPENCAI_ALLOWED_CLERK_USER_IDS"):
        return True
    bearer = request.headers.get("Authorization", "")
    if is_telegram_webapp_enabled() and bearer.startswith("Bearer "):
        session = verify_webapp_session_token(bearer[7:].strip())
        return bool(session and session["tg_uid"] in _allowlist("TELEGRAM_ALLOWED_USER_IDS"))
    return False


def _owns_generated_image(path: str, user_id: str) -> bool:
    with SessionLocal() as db:
        return db.scalar(
            select(GeneratedIllustration.path)
            .join(Conversation, GeneratedIllustration.conversation_id == Conversation.id)
            .where(
                Conversation.clerk_user_id == user_id,
                GeneratedIllustration.path == path,
            )
            .limit(1)
        ) is not None


async def enforce_private_beta(request: Request, call_next):
    path = normpath("/" + request.scope["path"].lstrip("/"))
    if request.method == "OPTIONS" or path in {"/api/health", "/api/auth/telegram/webapp"}:
        return await call_next(request)

    if path == "/api/gateway" or path.startswith("/api/gateway/"):
        return JSONResponse({"detail": "Gateway HTTP API is disabled"}, status_code=404)

    if path.startswith("/uploads/voice/"):
        return JSONResponse({"detail": "Audio file not found"}, status_code=404)

    generated_image = path.startswith("/uploads/generated/")
    if not generated_image and not path.startswith("/api/"):
        return await call_next(request)

    user_id = await get_current_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    if not await _beta_member(request, user_id):
        return JSONResponse({"detail": "Your account is not invited to this beta"}, status_code=403)
    if generated_image and not _owns_generated_image(path, user_id):
        return JSONResponse({"detail": "Image not found"}, status_code=404)

    request.state.opencai_user_id = user_id
    response = await call_next(request)
    response.headers["Cache-Control"] = "private, no-store"
    return response
