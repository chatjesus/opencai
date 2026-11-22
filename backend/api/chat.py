"""
Chat API – conversation management and streaming chat with AI characters.
"""
import asyncio
import json
import re
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional

from models.database import get_db, Character, CharacterTranslation, Conversation, GeneratedIllustration, Message
from models.schemas import (
    ConversationCreate, ConversationResponse, ConversationDetail,
    ChatMessageCreate, ChatMessageResponse,
    ConversationContextUpdate, IllustrateRequest,
)
from services.chat_service import generate_reply_stream, process_reply_images, StreamResult
from services.image_service import (
    ImageGenerationError,
    extract_image_tags,
    extract_scene_tags,
    generate_explicit_illustration,
    get_pregenerated_scenes,
    should_auto_generate_reply_images,
    strip_image_tags,
)
from services.intimacy_service import augment_image_prompt
from services.ops_config import get_ops_value, is_image_explicit_allowed
from services.scene_service import ensure_scenes_background
from auth.clerk import get_current_user_id, require_auth
from auth.private_beta import private_beta_enabled
from api.characters import require_character_visible

# Keywords that indicate the user wants to see an image / selfie
IMAGE_INTENT_RE = re.compile(
    r"(自拍|selfie|照片|photo|图片|picture|看看你|身材|胸部|胸|脸部|脸|腿|腰|"
    r"正面照|侧面|背面|全身|半身|show me|send me|发(一张|张|个)|"
    r"拍(一张|张|个)|take a pic|give me a pic|let me see you|show yourself|"
    r"your pic|your photo|your selfie|your body|your figure|your face|your chest)",
    re.IGNORECASE,
)

LOCALE_LANGUAGE = {
    "en": "English",
    "zh": "Simplified Chinese", "zh-TW": "Traditional Chinese",
    "ja": "Japanese", "ko": "Korean",
    "es": "Spanish", "fr": "French", "pt": "Portuguese",
    "de": "German", "ru": "Russian", "it": "Italian",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ar": "Arabic",
}

router = APIRouter(prefix="/api/chat", tags=["chat"])


# ── Conversation Management ──

@router.post("/conversations", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    data: ConversationCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Start a new conversation with a character."""
    char = db.query(Character).filter(Character.id == data.character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")

    user_id = await get_current_user_id(request)
    require_character_visible(char, user_id)
    conv = Conversation(
        character_id=data.character_id,
        title=f"Chat with {char.name}",
        clerk_user_id=None if user_id == "anonymous" else user_id,
        # Start hot if the character is an "established" persona (core-user fast path)
        intimacy_level=char.starting_intimacy or 0,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)

    # Trigger scene pre-generation in background (non-blocking)
    import asyncio
    asyncio.create_task(ensure_scenes_background(
        char.id, char.name, char.description or "",
        char.system_prompt or "", char.avatar_url,
    ))

    return conv


@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    request: Request,
    character_id: int = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """List conversations with character info, optionally filtered by character."""
    from sqlalchemy.orm import joinedload

    user_id = await get_current_user_id(request)
    query = db.query(Conversation).options(joinedload(Conversation.character))
    if private_beta_enabled():
        query = query.filter(Conversation.clerk_user_id == user_id)
    elif user_id != "anonymous":
        query = query.filter(
            (Conversation.clerk_user_id == user_id) |
            (Conversation.clerk_user_id == None)  # noqa: E711
        )
    else:
        # Anonymous users see ONLY anonymous conversations — never a logged-in
        # user's, which they can't open anyway (visibility check 403s) and which
        # would leak history + get mis-reused as "most recent".
        query = query.filter(Conversation.clerk_user_id == None)  # noqa: E711
    if character_id:
        query = query.filter(Conversation.character_id == character_id)
    convs = query.order_by(Conversation.updated_at.desc()).offset(skip).limit(limit).all()

    result = []
    for conv in convs:
        char = conv.character
        result.append(ConversationResponse(
            id=conv.id,
            character_id=conv.character_id,
            title=conv.title,
            character_name=char.name if char else "",
            character_avatar=char.avatar_url if char else "",
            intimacy_level=conv.intimacy_level or 0,
            streak_days=conv.streak_days or 0,
            created_at=conv.created_at,
            updated_at=conv.updated_at,
        ))
    return result


def _ensure_conv_visible(conv: Conversation, user_id: str) -> None:
    """A conversation is visible to its owner; anonymous-owned conversations
    are visible to anyone (no identity to verify against). Cross-user reads
    raise 403."""
    owner = conv.clerk_user_id
    if owner != user_id and (owner or private_beta_enabled()):
        raise HTTPException(status_code=403, detail="Not your conversation")


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Get a conversation with all its messages."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    user_id = await get_current_user_id(request)
    _ensure_conv_visible(conv, user_id)

    char = db.query(Character).filter(Character.id == conv.character_id).first()

    return ConversationDetail(
        id=conv.id,
        character_id=conv.character_id,
        character_name=char.name if char else "",
        character_avatar=char.avatar_url if char else "",
        title=conv.title,
        messages=[
            ChatMessageResponse(
                id=m.id, role=m.role, content=m.content,
                is_proactive=bool(getattr(m, "is_proactive", False)),
                created_at=m.created_at,
            )
            for m in conv.messages
        ],
        intimacy_level=conv.intimacy_level or 0,
        streak_days=conv.streak_days or 0,
        current_mood=getattr(conv, "current_mood", None),
        scenario_note=getattr(conv, "scenario_note", "") or "",
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Delete a conversation and all its messages."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    user_id = await get_current_user_id(request)
    _ensure_conv_visible(conv, user_id)
    db.query(GeneratedIllustration).filter_by(conversation_id=conversation_id).delete()
    db.query(Message).filter(Message.conversation_id == conversation_id).delete()
    db.delete(conv)
    db.commit()


# ── Chat / Streaming ──

@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: int,
    data: ChatMessageCreate,
    request: Request,
    locale: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Send a message in a conversation and stream the AI response back.
    Uses Server-Sent Events (SSE) for real-time streaming.
    Accepts ?locale= to inject language directive into the system prompt.
    """
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    user_id = await get_current_user_id(request)
    _ensure_conv_visible(conv, user_id)

    char = db.query(Character).filter(Character.id == conv.character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")
    require_character_visible(char, user_id)

    # Mark user activity so the proactive-recall timer (web_proactive) resets.
    # No explicit commit here: chat_service commits the conversation downstream
    # in this same session. An early commit would expire char/conv and break
    # the message_count / intimacy / streak writes that ride that later commit.
    conv.last_active_at = datetime.utcnow()

    # Build a request-scoped, localized prompt as a LOCAL string. We never
    # mutate `char.system_prompt` itself — doing so used to risk leaking
    # locale overlays back into the characters table on commit.
    localized_prompt: str | None = None
    if locale and locale not in ("zh", "zh-CN"):
        tr = db.query(CharacterTranslation).filter(
            CharacterTranslation.character_id == char.id,
            CharacterTranslation.locale == locale,
        ).first()
        if not tr and locale != "en":
            # Fallback to English
            tr = db.query(CharacterTranslation).filter(
                CharacterTranslation.character_id == char.id,
                CharacterTranslation.locale == "en",
            ).first()
        if tr and tr.system_prompt:
            localized_prompt = tr.system_prompt

    # Inject language directive so the model replies in user's language
    if locale and locale in LOCALE_LANGUAGE:
        base = localized_prompt if localized_prompt is not None else char.system_prompt
        lang = LOCALE_LANGUAGE[locale]
        directive = f"\n\n[IMPORTANT: Always reply in {lang}. Never switch languages.]"
        localized_prompt = base + directive

    async def event_stream():
        try:
            result = StreamResult()
            gen = generate_reply_stream(
                char, conv, data.content, db,
                user_id=user_id, result_holder=result,
                client_hour=data.client_hour,
                system_prompt_override=localized_prompt,
            )
            # Use a queue so keepalive timeouts never cancel the generator task
            _DONE = object()
            chunk_queue: asyncio.Queue = asyncio.Queue()

            async def _drain():
                try:
                    async for chunk in gen:
                        await chunk_queue.put(chunk)
                except Exception as exc:
                    await chunk_queue.put(exc)
                finally:
                    await chunk_queue.put(_DONE)

            drain_task = asyncio.create_task(_drain())

            while True:
                try:
                    item = await asyncio.wait_for(chunk_queue.get(), timeout=10.0)
                    if item is _DONE:
                        break
                    if isinstance(item, Exception):
                        raise item
                    yield f"data: {json.dumps({'content': item})}\n\n"
                except asyncio.TimeoutError:
                    if drain_task.done():
                        break
                    yield ": keepalive\n\n"

            await drain_task  # propagate any exception from the generator

            has_img = result.full_reply and extract_image_tags(result.full_reply)
            has_scene = result.full_reply and extract_scene_tags(result.full_reply)
            user_wants_image = bool(IMAGE_INTENT_RE.search(data.content))
            intimacy_level = conv.intimacy_level or 0

            # Auto-inject: user asked for image but LLM didn't produce any tag
            if user_wants_image and not (has_img or has_scene):
                scenes = get_pregenerated_scenes(char.id)
                if scenes:
                    # Rotate scenes based on message count to avoid showing same image twice
                    msg_count = db.query(Message).filter(
                        Message.conversation_id == conv.id
                    ).count()
                    # Avoid repeating the scene that was last shown (stored in last 3 msgs)
                    recent_content = " ".join(
                        m.content for m in db.query(Message)
                        .filter(Message.conversation_id == conv.id, Message.role == "assistant")
                        .order_by(Message.created_at.desc())
                        .limit(3)
                        .all()
                    )
                    used_indices = {i for i in scenes if f"scene_{i}" in recent_content}
                    available = {i: u for i, u in scenes.items() if i not in used_indices}
                    if not available:
                        available = scenes
                    idx = min(available.keys())
                    url = available[idx]
                    alt = f"{char.name}"
                    last_msg = (
                        db.query(Message)
                        .filter(Message.conversation_id == conv.id, Message.role == "assistant")
                        .order_by(Message.created_at.desc())
                        .first()
                    )
                    if last_msg:
                        last_msg.content = last_msg.content.rstrip() + f"\n\n![{alt}]({url})"
                        db.commit()
                    yield f"data: {json.dumps({'image': {'url': url, 'alt': alt}})}\n\n"

            elif has_img or has_scene:
                auto_img = bool(
                    has_img
                    and should_auto_generate_reply_images()
                    and get_ops_value(db, "nsfw_images_enabled", True)
                )
                if auto_img:
                    yield f"data: {json.dumps({'generating_image': True})}\n\n"

                if auto_img or has_scene:
                    instant, generated = await process_reply_images(
                        result.full_reply, conv.id, char.id, char.avatar_url, db,
                        intimacy_level=intimacy_level,
                        explicit_unlock_override=char.explicit_unlock_intimacy,
                    )
                    for img in instant:
                        yield f"data: {json.dumps({'image': img})}\n\n"
                    for img in generated:
                        yield f"data: {json.dumps({'image': img})}\n\n"

            # ── Tool Call Execution ──────────────────────────────────────────
            if result.tool_call:
                tool_name = result.tool_call.get("tool", "")
                tool_args = result.tool_call.get("args", {})

                # Notify frontend: tool is executing
                yield f"data: {json.dumps({'tool_executing': {'name': tool_name, 'args': tool_args}})}\n\n"

                try:
                    from actions.registry import get_tool_registry
                    registry = get_tool_registry()
                    tool_result = await registry.execute(tool_name, tool_args)

                    # Notify frontend: tool result received
                    yield f"data: {json.dumps({'tool_result': {'name': tool_name, 'success': tool_result.success, 'output': tool_result.output or tool_result.error or ''}})}\n\n"

                    if tool_result.success:
                        # Second LLM pass: incorporate tool result into character response
                        from services.llm_service import chat_completion
                        from services.chat_service import build_messages
                        followup_context = build_messages(char, conv, db, user_id=user_id)
                        # Append the tool result as context for the follow-up
                        followup_context.append({
                            "role": "assistant",
                            "content": result.full_reply or "好的，我来帮你查一下。"
                        })
                        followup_context.append({
                            "role": "system",
                            "content": (
                                f"工具「{tool_name}」执行完成，返回结果：\n{tool_result.output}\n\n"
                                f"现在以{char.name}的身份，用自然的口吻把这个结果告诉用户。"
                                f"保持角色人设，不要照抄原文，要像真人一样转述。"
                                f"如果结果包含链接，可以直接引用。"
                            ),
                        })
                        followup_text = await chat_completion(followup_context, max_tokens=400)
                        if followup_text:
                            # Save follow-up to DB (append to last assistant message)
                            from models.database import Message as DBMessage
                            last_msg = (
                                db.query(DBMessage)
                                .filter(DBMessage.conversation_id == conv.id, DBMessage.role == "assistant")
                                .order_by(DBMessage.created_at.desc())
                                .first()
                            )
                            if last_msg:
                                last_msg.content = (last_msg.content or "") + "\n\n" + followup_text
                                db.commit()
                            yield f"data: {json.dumps({'tool_followup': followup_text})}\n\n"

                except Exception as te:
                    yield f"data: {json.dumps({'tool_result': {'name': tool_name, 'success': False, 'output': str(te)}})}\n\n"

            # Send intimacy update event
            if result.intimacy_update:
                yield f"data: {json.dumps({'intimacy': result.intimacy_update})}\n\n"

            # Send streak update event (only on new-day milestones)
            if result.streak_update and result.streak_update.get("is_new_day"):
                su = result.streak_update
                milestone = su.get("milestone") or {}
                yield f"data: {json.dumps({'streak': {'streak_days': su['streak_days'], 'broken': su.get('broken', False), 'milestone_toast': milestone.get('toast'), 'intimacy_bonus': milestone.get('intimacy_bonus', 0)}})}\n\n"

            # ── Story Event Trigger Check ────────────────────────────────────
            try:
                from services.event_service import check_events
                event_data = check_events(conv, char, db)
                if event_data:
                    yield f"data: {json.dumps({'story_event': event_data})}\n\n"
            except Exception as ev_err:
                import logging
                logging.getLogger(__name__).warning(f"Event check error: {ev_err}")

            # ── TTS Voice Generation (non-blocking) ──────────────────────────
            try:
                from services.voice_service import synthesize_speech
                from services.ops_config import is_text_explicit_allowed
                display_text = result.full_reply or ""
                if display_text.strip() and not private_beta_enabled():
                    # Explicit lines skip cloud MiMo (it moderates them) → local TTS,
                    # so the voice matches the explicit text instead of degrading.
                    audio_url = await synthesize_speech(
                        text=display_text,
                        voice_id=char.voice_id or "",
                        tags=char.tags or "",
                        description=char.description or "",
                        explicit=is_text_explicit_allowed(
                            db, intimacy_level, char.explicit_unlock_intimacy),
                    )
                    if audio_url:
                        yield f"data: {json.dumps({'voice': {'url': audio_url}})}\n\n"
            except Exception as tts_err:
                import logging
                logging.getLogger(__name__).warning(f"TTS error: {tts_err}")

            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations/{conversation_id}/messages", response_model=list[ChatMessageResponse])
async def get_messages(
    conversation_id: int,
    request: Request,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """Get messages for a conversation."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    user_id = await get_current_user_id(request)
    _ensure_conv_visible(conv, user_id)

    messages = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return messages


@router.get("/conversations/{conversation_id}/poll", response_model=list[ChatMessageResponse])
async def poll_messages(
    conversation_id: int,
    request: Request,
    after_id: int = 0,
    db: Session = Depends(get_db),
):
    """Lightweight poll for messages newer than ``after_id``. An open chat tab
    calls this on an interval to surface proactive ('missed you') messages the
    scheduler staged while the user was idle, with no reload. Returns [] when
    nothing new."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    user_id = await get_current_user_id(request)
    _ensure_conv_visible(conv, user_id)

    rows = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.id > after_id)
        .order_by(Message.id.asc())
        .limit(50)
        .all()
    )
    return rows


@router.post("/conversations/{conversation_id}/checkin")
async def checkin(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Called when the user opens a chat. If they've been away long enough,
    the character proactively reaches out ('missed you'); the greeting is
    persisted as an assistant message and returned. Otherwise {greeting: null}.
    """
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    user_id = await get_current_user_id(request)
    _ensure_conv_visible(conv, user_id)

    char = db.query(Character).filter(Character.id == conv.character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")

    # Daily check-in reward (reciprocity/habit loop): showing up deepens the bond.
    from datetime import date
    from services.checkin import grant_daily_checkin
    checkin_reward = grant_daily_checkin(conv, db, date.today().isoformat())

    # The background scheduler (web_proactive) may have already staged a
    # "missed you" message while the user was away. If the newest message is
    # that unanswered proactive, don't greet a second time on open.
    newest = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.id.desc())
        .first()
    )
    already_reached_out = bool(
        newest and newest.role == "assistant" and getattr(newest, "is_proactive", False)
    )

    from services.proactive_greeting import generate_return_greeting
    greeting = None
    if not already_reached_out and not private_beta_enabled():
        greeting = await generate_return_greeting(char, conv, db)

    # The user is here now — reset the proactive-recall timer.
    conv.last_active_at = datetime.utcnow()
    db.commit()

    resp = {
        "greeting": None,
        "checkin_reward": checkin_reward,
        "intimacy_level": conv.intimacy_level or 0,
    }
    if greeting:
        msg = Message(conversation_id=conv.id, role="assistant", content=greeting)
        db.add(msg)
        db.commit()
        db.refresh(msg)
        resp["greeting"] = greeting
        resp["message_id"] = msg.id
    return resp


@router.post("/conversations/{conversation_id}/surprise")
async def surprise(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Daily surprise / gacha draw. One per calendar day: random rarity grants
    intimacy and a rarity-scaled in-character message (persisted). Returns
    {available: false} if already drawn today or the lever is off."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    user_id = await get_current_user_id(request)
    _ensure_conv_visible(conv, user_id)

    char = db.query(Character).filter(Character.id == conv.character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")

    from datetime import date
    from services.surprise import perform_surprise, generate_surprise_message
    reward = perform_surprise(conv, db, date.today().isoformat())
    if not reward:
        return {"available": False}

    message = await generate_surprise_message(char, conv, reward["rarity"], db)
    msg = Message(conversation_id=conv.id, role="assistant", content=message)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return {
        "available": True,
        "rarity": reward["rarity"],
        "intimacy_bonus": reward["intimacy_bonus"],
        "intimacy_level": conv.intimacy_level or 0,
        "message": message,
        "message_id": msg.id,
    }


# ── Persisted Scenario Context & Explicit Scene Illustration ─────────────────

_MD_IMAGE_STRIP_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_APPEARANCE_LINE_RE = re.compile(r"Appearance\s*[:：]\s*([^\n]+)", re.IGNORECASE)


def _build_turn_illustration_prompt(
    char: Character,
    conv: Conversation,
    recent_messages: list[Message],
    prompt_hint: Optional[str],
    intimacy_level: int,
    explicit_allowed: bool,
) -> tuple[str, str]:
    """
    Build a concise scene illustration prompt grounded in the character's traits
    and the conversation's latest turn / active scenario.
    Returns (final_prompt, short_alt_label).
    """
    last_assistant = next((m for m in recent_messages if m.role == "assistant"), None)
    last_user = next((m for m in recent_messages if m.role == "user"), None)

    appearance = ""
    if char.system_prompt:
        m = _APPEARANCE_LINE_RE.search(char.system_prompt)
        if m:
            appearance = m.group(1).strip()[:220]
    if not appearance and char.description:
        appearance = char.description.strip()[:180]

    # Reuse any unrendered [IMG: ...] description from the latest turn first
    extracted_tag = ""
    if last_assistant and last_assistant.content:
        tags = extract_image_tags(last_assistant.content)
        if tags:
            extracted_tag = tags[-1].strip()

    if extracted_tag:
        core_scene = extracted_tag
    else:
        scene_source = (
            last_assistant.content
            if (last_assistant and last_assistant.content)
            else (char.greeting or "")
        )
        clean_scene = strip_image_tags(_MD_IMAGE_STRIP_RE.sub("", scene_source)).strip()
        clean_scene = re.sub(r"\s+", " ", clean_scene)[:280]
        user_beat = ""
        if last_user and last_user.content:
            user_beat = re.sub(r"\s+", " ", last_user.content.strip())[:120]

        parts = [f"anime style story illustration of {char.name}"]
        if appearance:
            parts.append(appearance)
        if getattr(conv, "scenario_note", None):
            parts.append(f"Setting: {conv.scenario_note.strip()[:140]}")
        if clean_scene:
            parts.append(f"Current scene: {clean_scene}")
        if user_beat:
            parts.append(f"Moment context: {user_beat}")
        core_scene = ". ".join(p.rstrip(".") for p in parts if p)

    if prompt_hint and prompt_hint.strip():
        core_scene = f"{core_scene}. {prompt_hint.strip()[:200]}"

    effective_level = intimacy_level if explicit_allowed else min(intimacy_level, 20)
    augmented = augment_image_prompt(core_scene, effective_level)
    alt = f"{char.name} scene illustration"
    return augmented, alt


@router.put("/conversations/{conversation_id}/context")
async def update_conversation_context(
    conversation_id: int,
    data: ConversationContextUpdate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Persist user-edited scenario/story notes on the conversation across turns."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    user_id = await get_current_user_id(request)
    _ensure_conv_visible(conv, user_id)

    conv.scenario_note = (data.scenario_note or "").strip()
    conv.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(conv)
    return {
        "id": conv.id,
        "scenario_note": conv.scenario_note or "",
        "current_mood": conv.current_mood,
        "intimacy_level": conv.intimacy_level or 0,
        "streak_days": conv.streak_days or 0,
    }


@router.post("/conversations/{conversation_id}/illustrate")
async def illustrate_conversation_turn(
    conversation_id: int,
    request: Request,
    data: Optional[IllustrateRequest] = None,
    db: Session = Depends(get_db),
):
    """
    Explicitly generate one story illustration tied to the conversation's latest
    turn / current scene. Requires authentication, conversation ownership, and
    respects ops_config feature and intimacy gates.
    """
    user_id = await get_current_user_id(request)
    require_auth(user_id)

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    _ensure_conv_visible(conv, user_id)

    # Bind unowned anonymous draft conversation to the authenticated caller once used
    if conv.clerk_user_id is None:
        conv.clerk_user_id = user_id

    if not get_ops_value(db, "nsfw_images_enabled", True):
        raise HTTPException(status_code=403, detail="Image generation is currently disabled")

    char = db.query(Character).filter(Character.id == conv.character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")

    intimacy_level = conv.intimacy_level or 0
    require_character_visible(char, user_id)
    explicit_allowed = is_image_explicit_allowed(
        db, intimacy_level, char.explicit_unlock_intimacy
    )

    recent_messages = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.id.desc())
        .limit(6)
        .all()
    )

    prompt, alt = _build_turn_illustration_prompt(
        char=char,
        conv=conv,
        recent_messages=recent_messages,
        prompt_hint=data.prompt_hint if data else None,
        intimacy_level=intimacy_level,
        explicit_allowed=explicit_allowed,
    )

    try:
        url, provider = await generate_explicit_illustration(
            prompt=prompt,
            avatar_url=char.avatar_url,
            character_id=char.id,
            nsfw=explicit_allowed,
        )
    except ImageGenerationError as err:
        raise HTTPException(status_code=err.status_code, detail=err.detail)

    if url.startswith("/uploads/generated/"):
        db.add(GeneratedIllustration(path=url, conversation_id=conv.id))

    target_msg = next((m for m in recent_messages if m.role == "assistant"), None)
    if target_msg is not None:
        cleaned = strip_image_tags(target_msg.content).rstrip()
        target_msg.content = f"{cleaned}\n\n![{alt}]({url})" if cleaned else f"![{alt}]({url})"
        conv.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(target_msg)
    else:
        opening = (char.greeting or "").replace("{{char}}", char.name).replace("{{user}}", "You").strip()
        content = f"{opening}\n\n![{alt}]({url})" if opening else f"![{alt}]({url})"
        target_msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content=content,
        )
        db.add(target_msg)
        conv.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(target_msg)

    return {
        "image": {"url": url, "alt": alt},
        "message_id": target_msg.id,
        "provider": provider.value,
    }

