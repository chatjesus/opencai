import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.staticfiles import StaticFiles
from jose import jwt
from sqlalchemy.orm import sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

from auth import clerk, private_beta
from auth.telegram_webapp import TelegramAuthError, verify_telegram_init_data
from models.database import Character, Conversation, GeneratedIllustration, Message, get_db
from services import image_service, llm_service


def _mock_http(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw
    ))


@pytest.mark.asyncio
async def test_grok_completion_stream_cap_and_redacted_errors(monkeypatch):
    test_key = "unit-test-grok-secret"
    monkeypatch.setenv("LLM_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", test_key)
    requests = []

    def fake_upstream(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert str(request.url) == "https://api.x.ai/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {test_key}"
        if len(requests) == 3:
            return httpx.Response(429, text=test_key)
        if payload["stream"]:
            chunks = [
                {"choices": [{"delta": {"role": "assistant"}}]},
                {"choices": [{"delta": {"content": "雨停了。"}}]},
                {"choices": [{"delta": {"content": "她拉开窗。"}}]},
            ]
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            return httpx.Response(200, text=body + "data: [DONE]\n\n")
        return httpx.Response(200, json={"choices": [{"message": {"content": "她拉开窗。"}}]})

    _mock_http(monkeypatch, fake_upstream)
    messages = [{"role": "user", "content": "打开窗户"}]
    chunks = [part async for part in llm_service.chat_completion_stream(messages)]
    assert chunks == ["雨停了。", "她拉开窗。"]
    assert await llm_service.chat_completion(messages, max_tokens=32) == "她拉开窗。"
    assert [p["max_tokens"] for p in requests] == [600, 32]
    assert all(p["model"] == "grok-4.3" for p in requests)
    with pytest.raises(RuntimeError, match="429") as err:
        await llm_service.chat_completion(messages)
    assert test_key not in str(err.value)


@pytest.mark.asyncio
async def test_grok_image_is_explicit_and_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", "unit-test-grok-secret")
    monkeypatch.setattr(image_service, "BACKEND_DIR", tmp_path)
    monkeypatch.setattr(image_service, "GENERATED_DIR", tmp_path / "uploads" / "generated")
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jwioAAAAASUVORK5CYII="
    )
    requests = []

    def fake_upstream(request):
        assert str(request.url) == "https://api.x.ai/v1/images/generations"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(image).decode()}]})

    _mock_http(monkeypatch, fake_upstream)
    assert not image_service.should_auto_generate_reply_images()
    assert requests == []
    url, provider = await image_service.generate_explicit_illustration("Rain outside the studio")
    assert provider == image_service.Provider.XAI
    assert (tmp_path / url.lstrip("/")).read_bytes() == image
    assert requests == [{
        "model": "grok-imagine-image-2.0", "prompt": "Rain outside the studio",
        "n": 1, "resolution": "1k", "quality": "low", "aspect_ratio": "1:1",
        "response_format": "b64_json",
    }]


def _telegram_data(token, timestamp, user_id):
    fields = {"auth_date": str(timestamp), "user": json.dumps({"id": user_id})}
    joined = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
    fields["hash"] = hmac.new(secret, joined.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_telegram_init_data_freshness_and_allowlist(monkeypatch):
    token = "12345:test-bot-token"
    now = int(time.time())
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("TELEGRAM_WEBAPP_ALLOWLIST_REQUIRED", "true")
    good = _telegram_data(token, now, 123)
    assert verify_telegram_init_data(good, bot_token=token, now_ts=now).telegram_user_id == 123
    for invalid, expected in [
        (good + "&auth_date=884", "duplicate_field"),
        (good.replace("123", "999"), "bad_signature"),
        (_telegram_data(token, now - 3600, 123), "expired_init_data"),
        (_telegram_data(token, now, 999), "not_allowlisted"),
    ]:
        with pytest.raises(TelegramAuthError) as err:
            verify_telegram_init_data(invalid, bot_token=token, now_ts=now)
        assert err.value.code == expected


@pytest.mark.asyncio
async def test_clerk_rejects_unconfigured_issuer_without_network(monkeypatch):
    monkeypatch.setenv("CLERK_JWT_ISSUER", "https://auth.opencai.date")
    token = jwt.encode({"iss": "https://untrusted.invalid", "sub": "user_other"}, "test")
    def reject_network(_request):
        pytest.fail("Should never request the untrusted issuer")
    _mock_http(monkeypatch, reject_network)
    assert await clerk.verify_clerk_token(token) is None


def test_private_beta_requires_invite_and_owns_generated_media(monkeypatch, engine, db):
    monkeypatch.setenv("OPENCAI_ALLOWED_CLERK_USER_IDS", "user_a,user_b")
    monkeypatch.setattr(private_beta, "SessionLocal", sessionmaker(bind=engine))
    async def fake_auth(token):
        return token
    monkeypatch.setattr(clerk, "verify_clerk_token", fake_auth)
    db.add(Character(id=1, name="沈棠", system_prompt="电台制片人"))
    db.add(Conversation(id=1, character_id=1, clerk_user_id="user_a"))
    path = "/uploads/generated/story_image.png"
    db.add(GeneratedIllustration(path=path, conversation_id=1))
    db.add(Conversation(id=2, character_id=1, clerk_user_id="user_b"))
    db.add(Message(conversation_id=2, role="assistant", content=f"![story]({path})"))
    db.commit()
    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=private_beta.enforce_private_beta)
    app.get("/api/characters")(lambda: [])
    app.get(path)(lambda: {})
    with TestClient(app) as client:
        assert client.get("/api/characters").status_code == 401
        assert client.get("/api/characters", headers={"Authorization": "Bearer user_other"}).status_code == 403
        assert client.get("/api/gateway/sessions", headers={"Authorization": "Bearer user_a"}).status_code == 404
        assert client.get(path, headers={"Authorization": "Bearer user_b"}).status_code == 404
        image = client.get(path, headers={"Authorization": "Bearer user_a"})
        assert image.status_code == 200
        assert image.headers["Cache-Control"] == "private, no-store"


def test_web_and_signed_telegram_share_grok_chat_and_illustration(monkeypatch, engine, db, tmp_path):
    from api.auth import router as auth_router
    from api.chat import router as chat_router

    monkeypatch.setenv("LLM_PROVIDER", "xai")
    monkeypatch.setenv("IMAGE_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", "unit-test-grok-secret")
    monkeypatch.setenv("OPENCAI_ALLOWED_CLERK_USER_IDS", "user_a,user_b")
    monkeypatch.setenv("TELEGRAM_WEBAPP_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_POLLING_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:test-bot-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("models.database.SessionLocal", Session)
    monkeypatch.setattr(private_beta, "SessionLocal", Session)
    async def fake_clerk(token):
        return token if token in {"user_a", "user_b"} else None
    monkeypatch.setattr(clerk, "verify_clerk_token", fake_clerk)
    db.add(Character(id=1, name="沈棠", system_prompt="电台制片人", greeting="夜深了。"))
    db.commit()

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(image_service, "BACKEND_DIR", tmp_path)
    monkeypatch.setattr(image_service, "GENERATED_DIR", upload_dir / "generated")
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jwioAAAAASUVORK5CYII="
    outputs = []
    scenario = "三楼的钥匙在旧电台。"

    def fake_upstream(request):
        payload = json.loads(request.content)
        if str(request.url) == llm_service.XAI_CHAT_COMPLETIONS_URL:
            if payload["stream"]:
                assert any(scenario in item["content"] for item in payload["messages"])
                outputs.append("chat")
                chunk = json.dumps({"choices": [{"delta": {"content": "窗外的雨停了。"}}]})
                return httpx.Response(200, text=f"data: {chunk}\n\ndata: [DONE]\n\n")
            return httpx.Response(200, json={"choices": [{"message": {"content": "[]" }}]})
        assert str(request.url) == image_service.XAI_IMAGES_URL
        assert scenario in payload["prompt"]
        outputs.append("image")
        return httpx.Response(200, json={"data": [{"b64_json": png}]})

    _mock_http(monkeypatch, fake_upstream)
    app = FastAPI()
    app.add_middleware(BaseHTTPMiddleware, dispatch=private_beta.enforce_private_beta)
    app.include_router(auth_router)
    app.include_router(chat_router)
    app.mount("/uploads", StaticFiles(directory=upload_dir))
    def test_db():
        with Session() as session:
            yield session
    app.dependency_overrides[get_db] = test_db

    web = {"Authorization": "Bearer user_a"}
    other_user = {"Authorization": "Bearer user_b"}
    with TestClient(app) as client:
        launch = client.post("/api/auth/telegram/webapp", json={
            "init_data": _telegram_data("12345:test-bot-token", int(time.time()), 123),
        })
        assert launch.status_code == 200, launch.text
        unlinked = {"Authorization": f"Bearer {launch.json()['access_token']}"}
        unlinked_save = client.post("/api/chat/conversations", headers=unlinked, json={"character_id": 1})
        assert unlinked_save.status_code == 201, unlinked_save.text
        conversation_id = unlinked_save.json()["id"]
        path = f"/api/chat/conversations/{conversation_id}"
        assert client.put(f"{path}/context", headers=unlinked, json={"scenario_note": scenario}).status_code == 200
        stream = client.post(f"{path}/messages", headers=unlinked, json={"content": "打开窗户。"})
        assert stream.status_code == 200, stream.text
        assert outputs == ["chat"]
        assert "窗外的雨停了。" in client.get(path, headers=unlinked).json()["messages"][-1]["content"]
        illustration = client.post(f"{path}/illustrate", headers=unlinked, json={})
        assert illustration.status_code == 200, illustration.text
        assert outputs == ["chat", "image"]
        image_url = illustration.json()["image"]["url"]

        code = client.post("/api/auth/bind/code", headers=web).json()["code"]
        linked = client.post("/api/auth/telegram/bind", json={"code": code}, headers=unlinked)
        assert linked.status_code == 200, linked.text
        telegram = {"Authorization": f"Bearer {linked.json()['access_token']}"}
        assert linked.json()["user_id"] == "user_a"
        assert client.get(path, headers=web).json() == client.get(path, headers=telegram).json()
        assert image_url in client.get(path, headers=web).json()["messages"][-1]["content"]
        assert client.get(image_url, headers=telegram).content == base64.b64decode(png)
        assert client.get(image_url).status_code == 401
        assert client.get(image_url, headers=other_user).status_code == 404
        assert client.post("/api/auth/telegram/bind", json={"code": code}, headers=telegram).status_code != 200
