"""
Settings API: manage platform configuration (Telegram, Discord, etc.)

Security boundary:
  - Critical Telegram settings (bot token, allowlist, Mini App flags) take
    precedence from server environment variables.
  - Mutations (`PUT /api/settings/telegram`, `POST /api/settings/telegram/test`)
    require `OPS_ADMIN_TOKEN` to be set on the server and matched via
    constant-time `X-Admin-Token` header comparison. When `OPS_ADMIN_TOKEN` is
    unset, Telegram settings are strictly environment-only.
  - Bot tokens and allowlist IDs are never exposed to unauthenticated callers.
"""
import hmac
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "platform_config.json")

_telegram_app = None


def _is_valid_admin(request: Request) -> bool:
    admin_token = os.environ.get("OPS_ADMIN_TOKEN", "").strip()
    if not admin_token:
        return False
    provided = request.headers.get("X-Admin-Token", "")
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), admin_token.encode("utf-8"))


def _require_settings_admin(request: Request) -> None:
    admin_token = os.environ.get("OPS_ADMIN_TOKEN", "").strip()
    if not admin_token:
        raise HTTPException(
            status_code=403,
            detail="Telegram settings mutations are disabled (environment-only unless OPS_ADMIN_TOKEN is set).",
        )
    if not _is_valid_admin(request):
        raise HTTPException(status_code=403, detail="Admin token required")


def _load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "telegram": {
            "bot_token": "",
            "enabled": False,
            "default_character_id": 1,
            "allowlist_mode": True,
            "allowed_user_ids": [],
        },
        "discord": {
            "bot_token": "",
            "enabled": False,
        },
    }


def _save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


class TelegramConfig(BaseModel):
    bot_token: str = ""
    enabled: bool = False
    default_character_id: int = 1
    allowlist_mode: bool = True
    allowed_user_ids: list[int] = []


class PlatformStatus(BaseModel):
    platform: str
    enabled: bool
    connected: bool
    bot_username: Optional[str] = None
    error: Optional[str] = None


def _effective_telegram_state() -> dict:
    cfg = _load_config().get("telegram", {})
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    env_allowed_raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    env_webapp = os.environ.get("TELEGRAM_WEBAPP_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    env_webapp_url = os.environ.get("TELEGRAM_WEBAPP_URL", "").strip()

    env_allowed = [
        int(x.strip())
        for x in env_allowed_raw.split(",")
        if x.strip().isdigit()
    ] if env_allowed_raw else []
    saved_allowed = [
        int(x) for x in (cfg.get("allowed_user_ids") or [])
        if isinstance(x, int) and not isinstance(x, bool)
    ]
    merged_allowed = sorted(set(env_allowed) | set(saved_allowed))
    token = env_token or str(cfg.get("bot_token", "")).strip()
    allowlist_mode = bool(
        env_allowed
        or os.environ.get("TELEGRAM_ALLOWLIST_MODE", "").strip().lower() in ("1", "true", "yes", "on")
        or cfg.get("allowlist_mode", False)
    )
    return {
        "token": token,
        "token_from_env": bool(env_token),
        "enabled": bool(cfg.get("enabled", False) or (env_token and os.environ.get("TELEGRAM_POLLING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"))),
        "default_character_id": int(cfg.get("default_character_id", 1) or 1),
        "allowlist_mode": allowlist_mode,
        "allowed_user_ids": merged_allowed,
        "webapp_enabled": env_webapp,
        "webapp_url": env_webapp_url,
        "mutations_locked": not bool(os.environ.get("OPS_ADMIN_TOKEN", "").strip()),
    }


# ── GET all settings ────────────────────────────────────────

@router.get("/platforms")
def get_platform_settings(request: Request):
    """Get platform configurations with secrets redacted."""
    config = _load_config()
    is_admin = _is_valid_admin(request)
    tg_state = _effective_telegram_state()
    result = {}
    for platform, cfg in config.items():
        if platform == "telegram":
            result["telegram"] = {
                "bot_token_set": bool(tg_state["token"]),
                "bot_token_masked": "***" if tg_state["token"] else "",
                "enabled": tg_state["enabled"],
                "default_character_id": tg_state["default_character_id"],
                "allowlist_mode": tg_state["allowlist_mode"],
                "allowed_user_ids": tg_state["allowed_user_ids"] if is_admin else [],
                "webapp_enabled": tg_state["webapp_enabled"],
                "mutations_locked": tg_state["mutations_locked"],
            }
            continue
        masked = dict(cfg)
        has_tok = bool(masked.get("bot_token"))
        masked["bot_token_masked"] = "***" if has_tok else ""
        masked["bot_token_set"] = has_tok
        masked.pop("bot_token", None)
        result[platform] = masked
    return result


# ── Telegram ────────────────────────────────────────────────

@router.get("/telegram")
def get_telegram_config(request: Request):
    """Get Telegram configuration (tokens and non-admin allowlists redacted)."""
    state = _effective_telegram_state()
    is_admin = _is_valid_admin(request)
    return {
        "bot_token_set": bool(state["token"]),
        "bot_token_masked": "***" if state["token"] else "",
        "token_from_env": state["token_from_env"],
        "enabled": state["enabled"],
        "default_character_id": state["default_character_id"],
        "allowlist_mode": state["allowlist_mode"],
        "allowed_user_ids": state["allowed_user_ids"] if is_admin else [],
        "webapp_enabled": state["webapp_enabled"],
        "webapp_url": state["webapp_url"] if is_admin else "",
        "mutations_locked": state["mutations_locked"],
    }


@router.put("/telegram")
async def update_telegram_config(data: TelegramConfig, request: Request):
    """Update Telegram configuration (admin-gated; env vars take precedence)."""
    _require_settings_admin(request)
    config = _load_config()
    tg_cfg = config.setdefault("telegram", {})

    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if data.bot_token and env_token:
        raise HTTPException(
            status_code=400,
            detail="TELEGRAM_BOT_TOKEN is managed via environment and cannot be overwritten via API.",
        )
    if data.bot_token:
        tg_cfg["bot_token"] = data.bot_token.strip()

    tg_cfg["enabled"] = data.enabled
    tg_cfg["default_character_id"] = data.default_character_id
    tg_cfg["allowlist_mode"] = data.allowlist_mode
    tg_cfg["allowed_user_ids"] = [int(uid) for uid in data.allowed_user_ids if int(uid) > 0]
    _save_config(config)

    effective_token = env_token or tg_cfg.get("bot_token", "")
    if data.enabled and effective_token:
        result = await _start_telegram(effective_token, data.default_character_id)
        return result
    await _stop_telegram()
    return {"status": "disabled", "connected": False}


@router.post("/telegram/test")
async def test_telegram_token(data: dict, request: Request):
    """Test a Telegram bot token without saving it (admin-only)."""
    _require_settings_admin(request)
    token = str(data.get("bot_token") or "").strip()
    if not token:
        raise HTTPException(400, "bot_token is required")

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        if resp.status_code == 200:
            bot_info = resp.json().get("result", {})
            return {
                "valid": True,
                "bot_username": bot_info.get("username", ""),
                "bot_name": bot_info.get("first_name", ""),
            }
        return {"valid": False, "error": "Invalid token"}
    except Exception:
        return {"valid": False, "error": "Failed to reach Telegram API"}


@router.get("/telegram/status")
async def get_telegram_status():
    """Get current Telegram bot connection status without exposing credentials."""
    global _telegram_app
    state = _effective_telegram_state()

    if not state["token"]:
        return PlatformStatus(platform="telegram", enabled=False, connected=False)

    if _telegram_app:
        try:
            bot_info = await _telegram_app.bot.get_me()
            return PlatformStatus(
                platform="telegram",
                enabled=state["enabled"],
                connected=True,
                bot_username=f"@{bot_info.username}",
            )
        except Exception:
            return PlatformStatus(
                platform="telegram",
                enabled=state["enabled"],
                connected=False,
                error="Bot connection check failed",
            )

    return PlatformStatus(
        platform="telegram",
        enabled=state["enabled"],
        connected=False,
    )


async def _start_telegram(token: str, default_character_id: int = 1) -> dict:
    """Start the Telegram bot with the given token."""
    global _telegram_app
    await _stop_telegram()

    try:
        from channels.telegram.config import TELEGRAM_CONFIG
        state = _effective_telegram_state()
        TELEGRAM_CONFIG["default_character_id"] = default_character_id
        TELEGRAM_CONFIG["allowlist_mode"] = state["allowlist_mode"]
        TELEGRAM_CONFIG["allowed_user_ids"] = state["allowed_user_ids"]

        import channels.telegram.config as tg_cfg
        tg_cfg.TELEGRAM_BOT_TOKEN = token

        from channels.telegram.adapter import start_telegram_polling
        _telegram_app = await start_telegram_polling()
        if _telegram_app:
            try:
                import asyncio as _aio
                bot_info = await _aio.wait_for(_telegram_app.bot.get_me(), timeout=8.0)
                username = f"@{bot_info.username}"
            except Exception:
                username = "(unknown)"
            logger.info("Telegram bot started: %s", username)
            return {
                "status": "connected",
                "connected": True,
                "bot_username": username,
            }
        return {"status": "failed", "connected": False, "error": "App creation returned None"}
    except Exception:
        logger.error("Telegram start failed")
        return {"status": "failed", "connected": False, "error": "Failed to start Telegram bot"}


async def _stop_telegram():
    """Stop the running Telegram bot if any."""
    global _telegram_app
    if _telegram_app:
        try:
            from channels.telegram.adapter import stop_telegram
            await stop_telegram(_telegram_app)
        except Exception as e:
            logger.warning("Telegram stop error: %s", type(e).__name__)
        _telegram_app = None


async def auto_start_telegram():
    """Called on app startup to auto-connect if previously configured."""
    state = _effective_telegram_state()
    if state["enabled"] and state["token"]:
        logger.info("Auto-starting Telegram bot from configuration...")
        await _start_telegram(state["token"], state["default_character_id"])
