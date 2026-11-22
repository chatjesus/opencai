"""
Auth API – basic JWT authentication, Telegram Mini App verification, and
canonical Web <-> Telegram account linkage.
"""
from datetime import datetime, timedelta
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.clerk import get_current_user_id, require_auth
from auth.telegram_webapp import (
    TelegramAuthError,
    create_telegram_bind_code,
    create_webapp_session_token,
    is_telegram_webapp_enabled,
    redeem_telegram_bind_code,
    resolve_telegram_canonical_user,
    verify_telegram_init_data,
    verify_webapp_session_token,
)
from models.database import User, get_db
from models.schemas import Token, UserCreate, UserResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Legacy local JWT settings
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "synclub-local-secret-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 72

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class TelegramWebAppAuthRequest(BaseModel):
    init_data: str = Field(..., min_length=1, max_length=8192)


class TelegramBindCodeRedeemRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=128)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


@router.post("/register", response_model=UserResponse, status_code=201)
def register(data: UserCreate, db: Session = Depends(get_db)):
    """Register a new user."""
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(400, "Username already taken")
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(400, "Email already registered")

    user = User(
        username=data.username,
        email=data.email,
        hashed_password=pwd_context.hash(data.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=Token)
def login(data: UserCreate, db: Session = Depends(get_db)):
    """Login and get JWT token."""
    user = db.query(User).filter(User.username == data.username).first()
    if not user or not pwd_context.verify(data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    token = create_access_token({"sub": str(user.id), "username": user.username})
    return Token(access_token=token)


# ── Telegram Mini App Auth & Canonical Account Linkage ───────────────────────

@router.post("/telegram/webapp")
def authenticate_telegram_webapp(
    payload: TelegramWebAppAuthRequest,
    db: Session = Depends(get_db),
):
    """
    Verify Telegram Mini App ``initData`` (HMAC-SHA256 + freshness + allowlist)
    and issue a scoped backend session JWT bound to the canonical user identity.
    Never logs ``init_data`` or issued tokens.
    """
    if not is_telegram_webapp_enabled():
        raise HTTPException(
            status_code=403,
            detail="Telegram Mini App authentication is not enabled on this server",
        )

    try:
        tg_user = verify_telegram_init_data(payload.init_data)
        canonical_user_id, is_linked = resolve_telegram_canonical_user(
            db, tg_user.telegram_user_id
        )
        session_token, ttl = create_webapp_session_token(
            canonical_user_id=canonical_user_id,
            telegram_user_id=tg_user.telegram_user_id,
        )
    except TelegramAuthError as err:
        raise HTTPException(status_code=err.status_code, detail=err.detail)

    return {
        "access_token": session_token,
        "token_type": "bearer",
        "user_id": canonical_user_id,
        "telegram_user_id": str(tg_user.telegram_user_id),
        "linked": is_linked,
        "expires_in": ttl,
    }


@router.post("/bind/code")
async def generate_bind_code(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Generate a single-use, short-lived (10 min) bind code for an authenticated
    Web user to link their Telegram account via ``/bind <code>`` or the Mini App.
    """
    user_id = await get_current_user_id(request)
    require_auth(user_id)
    if user_id.startswith("tg:"):
        raise HTTPException(
            status_code=400,
            detail="Sign in on Web first to generate an account link code",
        )

    try:
        code, ttl = create_telegram_bind_code(db, user_id, ttl_seconds=600)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    return {
        "code": code,
        "expires_in": ttl,
    }


@router.post("/telegram/bind")
def bind_telegram_webapp_session(
    payload: TelegramBindCodeRedeemRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Redeem a single-use Web bind code from inside an authenticated Telegram Mini App
    session and return an updated session token carrying the canonical Web owner.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Telegram Mini App session required")
    raw_bearer = auth_header[7:].strip()
    verified = verify_webapp_session_token(raw_bearer, db=db)
    if not verified:
        raise HTTPException(status_code=401, detail="Invalid or expired Telegram Mini App session")

    tg_uid = verified["tg_uid"]
    try:
        canonical_user_id = redeem_telegram_bind_code(db, tg_uid, payload.code)
        new_token, ttl = create_webapp_session_token(
            canonical_user_id=canonical_user_id,
            telegram_user_id=tg_uid,
        )
    except TelegramAuthError as err:
        raise HTTPException(status_code=err.status_code, detail=err.detail)

    return {
        "access_token": new_token,
        "token_type": "bearer",
        "user_id": canonical_user_id,
        "telegram_user_id": tg_uid,
        "linked": True,
        "expires_in": ttl,
    }


@router.get("/me")
async def get_auth_me(
    request: Request,
    db: Session = Depends(get_db),
):
    """Non-secret session introspection for Web and Telegram Mini App clients."""
    user_id = await get_current_user_id(request)
    auth_header = request.headers.get("Authorization", "")
    tg_uid = None
    linked = False
    if auth_header.startswith("Bearer "):
        webapp = verify_webapp_session_token(auth_header[7:].strip(), db=db)
        if webapp:
            tg_uid = webapp["tg_uid"]
            _, linked = resolve_telegram_canonical_user(db, tg_uid)
    return {
        "authenticated": user_id != "anonymous",
        "user_id": None if user_id == "anonymous" else user_id,
        "telegram_user_id": tg_uid,
        "linked": linked,
        "telegram_webapp_enabled": is_telegram_webapp_enabled(),
    }
