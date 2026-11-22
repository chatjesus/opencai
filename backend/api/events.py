"""
Events API — story event management.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db, Conversation, Character, ConversationEvent, CharacterEvent
from services.event_service import resolve_choice, get_pending_events
from auth.clerk import get_current_user_id
from auth.private_beta import private_beta_enabled

router = APIRouter(prefix="/api/events", tags=["events"])


class ChoiceRequest(BaseModel):
    instance_id: int
    choice_index: int
    conversation_id: int


@router.post("/choose")
async def make_choice(req: ChoiceRequest, request: Request, db: Session = Depends(get_db)):
    """User makes a choice for an active story event."""
    conv = db.query(Conversation).filter(Conversation.id == req.conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if private_beta_enabled() and conv.clerk_user_id != await get_current_user_id(request):
        raise HTTPException(status_code=404, detail="Conversation not found")

    char = db.query(Character).filter(Character.id == conv.character_id).first()
    if not char:
        raise HTTPException(status_code=404, detail="Character not found")

    instance = db.query(ConversationEvent).filter_by(
        id=req.instance_id, conversation_id=conv.id, status="active"
    ).first()
    if not instance:
        raise HTTPException(status_code=404, detail="Active story event not found")
    result = await resolve_choice(
        instance_id=req.instance_id,
        choice_index=req.choice_index,
        conversation=conv,
        character=char,
        db=db,
    )
    return result


@router.get("/pending/{conversation_id}")
async def get_pending(conversation_id: int, request: Request, db: Session = Depends(get_db)):
    """Get all active (awaiting choice) events for a conversation."""
    if private_beta_enabled():
        conv = db.get(Conversation, conversation_id)
        if not conv or conv.clerk_user_id != await get_current_user_id(request):
            raise HTTPException(status_code=404, detail="Conversation not found")
    return get_pending_events(conversation_id, db)


@router.get("/list/{character_id}")
def list_character_events(character_id: int, db: Session = Depends(get_db)):
    """List all story events defined for a character (for debugging/preview)."""
    events = (
        db.query(CharacterEvent)
        .filter(CharacterEvent.char_id == character_id)
        .order_by(CharacterEvent.sort_order.asc())
        .all()
    )
    import json
    return [
        {
            "id": e.id,
            "title": e.title,
            "event_type": e.event_type,
            "trigger": json.loads(e.trigger_json or "{}"),
            "choices": json.loads(e.choices_json or "[]"),
            "sort_order": e.sort_order,
        }
        for e in events
    ]
