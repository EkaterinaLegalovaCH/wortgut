"""JSON endpoints: word lookup, word saving, and the health checks.

M0 deliberately has no authentication and binds to localhost only; the lookup
path never touches the network, so every answer works offline.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_engine, get_session
from ..lookup import lookup, save_user_word
from ..models import DictEntry, TextRecord
from ..pipeline import is_model_installed

router = APIRouter()


class WordIn(BaseModel):
    lemma: str = Field(min_length=1, max_length=200)
    translation: str = Field(default="", max_length=500)
    text_id: int
    position: int = Field(ge=0)


class HealthOut(BaseModel):
    status: str


class ReadyOut(BaseModel):
    ready: bool
    checks: dict[str, bool]
    missing: list[str]


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    """Cheap liveness probe: process is up and serving."""
    return HealthOut(status="ok")


@router.get("/ready", response_model=ReadyOut)
def ready() -> ReadyOut:
    """Readiness: everything the app needs to do real work is present."""
    checks: dict[str, bool] = {}

    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database_reachable"] = True
    except Exception:  # noqa: BLE001 — any failure means "not ready", and we report it
        checks["database_reachable"] = False

    try:
        with Session(get_engine()) as session:
            checks["dictionary_loaded"] = (
                session.scalar(select(func.count()).select_from(DictEntry)) or 0
            ) > 0
    except Exception:  # noqa: BLE001
        checks["dictionary_loaded"] = False

    checks["spacy_model_loaded"] = is_model_installed()

    missing = [name for name, ok in checks.items() if not ok]
    return ReadyOut(ready=not missing, checks=checks, missing=missing)


@router.get("/api/lookup")
def api_lookup(text_id: int, position: int, session: Session = Depends(get_session)):
    """Everything the word popup shows: surface, lemma, translations, rank."""
    result = lookup(session, text_id, position)
    if result is None:
        raise HTTPException(status_code=404, detail="Token not found")

    return {
        "surface": result.surface,
        "lemma": result.lemma,
        "pos": result.pos,
        "translations": result.translations,
        "frequency_rank": result.frequency_rank,
        "frequency_count": result.frequency_count,
        "already_saved": result.already_saved,
        "saved_translation": result.saved_translation,
    }


@router.post("/api/words", status_code=201)
def api_save_word(payload: WordIn, session: Session = Depends(get_session)):
    """Save a word with the sentence it appeared in.

    A missing dictionary entry is expected, not an error: the user can type a
    translation by hand and save the word anyway.
    """
    if session.get(TextRecord, payload.text_id) is None:
        raise HTTPException(status_code=404, detail="Text not found")

    word = save_user_word(
        session,
        lemma=payload.lemma,
        translation=payload.translation,
        text_id=payload.text_id,
        position=payload.position,
    )
    return {
        "id": word.id,
        "lemma": word.lemma,
        "translation": word.translation,
        "encounters": word.encounters,
        "context_sentence": word.context_sentence,
        "target_lang": get_settings().target_lang,
    }
