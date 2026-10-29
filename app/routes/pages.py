"""Page routes: home, text creation, reader, word list.

These render HTML. JSON endpoints live in app/routes/api.py.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..knowledge import text_coverage
from ..lookup import delete_user_word, list_texts
from ..models import TextRecord, Token, UserWord
from ..pipeline import ModelNotInstalled, tokenize

router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


@router.get("/")
def home(request: Request, session: Session = Depends(get_session)):
    texts = list_texts(session)
    # Coverage per listed text: a handful of queries for a handful of texts, and
    # it is the number that decides whether a text is worth reading right now.
    coverage = {text.id: text_coverage(session, text.id) for text, _ in texts}
    return _templates(request).TemplateResponse(
        request, "home.html", {"texts": texts, "coverage": coverage}
    )


@router.post("/texts")
def create_text(
    request: Request,
    text: str = Form(...),
    title: str = Form(""),
    source_note: str = Form(""),
    session: Session = Depends(get_session),
):
    """Run the spaCy pipeline over the pasted text and store text + tokens."""
    if not text.strip():
        raise HTTPException(status_code=422, detail="Text must not be empty.")

    try:
        drafts = tokenize(text.strip())
    except ModelNotInstalled as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    text_record = TextRecord(
        title=title.strip() or _title_from(text),
        source_note=source_note.strip() or None,
    )
    text_record.tokens = [
        Token(
            position=draft.position,
            surface=draft.surface,
            lemma=draft.lemma,
            pos=draft.pos,
            is_word=draft.is_word,
            whitespace=draft.whitespace,
        )
        for draft in drafts
    ]
    session.add(text_record)
    session.commit()
    return RedirectResponse(url=f"/texts/{text_record.id}", status_code=303)


def _title_from(text: str) -> str:
    """Fallback title: the first few words, so the text list is never blank."""
    words = text.strip().split()
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "")


@router.get("/texts/{text_id}")
def reader(text_id: int, request: Request, session: Session = Depends(get_session)):
    text_record = session.get(TextRecord, text_id)
    if text_record is None:
        raise HTTPException(status_code=404, detail="Text not found")

    tokens = session.scalars(
        select(Token).where(Token.text_id == text_id).order_by(Token.position)
    ).all()
    saved_lemmas = set(session.scalars(select(UserWord.lemma)).all())

    # Shape the rows for the template: one entry per token, with everything the
    # reading view needs to draw it.
    rendered = [
        {
            "position": token.position,
            "surface": token.surface,
            "lemma": token.lemma,
            "is_word": token.is_word,
            "whitespace": token.whitespace,
            "saved": token.lemma in saved_lemmas,
        }
        for token in tokens
    ]

    return _templates(request).TemplateResponse(
        request,
        "reader.html",
        {
            "text": text_record,
            "rendered": rendered,
            "coverage": text_coverage(session, text_id),
        },
    )


@router.get("/words")
def words(request: Request, session: Session = Depends(get_session)):
    entries = session.scalars(select(UserWord).order_by(UserWord.added_at.desc())).all()
    return _templates(request).TemplateResponse(request, "words.html", {"words": entries})


@router.post("/words/{word_id}/delete")
def delete_word(word_id: int, session: Session = Depends(get_session)):
    delete_user_word(session, word_id)
    return RedirectResponse(url="/words", status_code=303)
