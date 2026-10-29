"""Placement check pages: show the items, score the answers, show the result.

Form handling note: the item fields are named by position (`item0`, `item1`, …),
so the form is read with `request.form()` and mapped back by index. FastAPI's
declared `Form(...)` parameters are a poor fit for a list whose length is only
known at runtime, and the mapping stays explicit this way.
"""

from __future__ import annotations

import random

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import PlacementResponse, PlacementRun
from ..placement import (
    DISCLAIMER,
    Item,
    latest_run,
    responses_for,
    save_run,
    score,
    select_items,
)

router = APIRouter()

DEFAULT_SEED_LIMIT = 1_000_000


def _templates(request: Request):
    return request.app.state.templates


def _seed_from(request: Request) -> int:
    """Use ?seed= when given (reproducible runs), else a fresh random seed."""
    raw = request.query_params.get("seed")
    if raw and raw.isdigit():
        return int(raw) % DEFAULT_SEED_LIMIT
    return random.randint(1, DEFAULT_SEED_LIMIT)


@router.get("/placement")
def placement_form(request: Request, session: Session = Depends(get_session)):
    seed = _seed_from(request)
    items = select_items(session, seed)

    if not any(not item.is_nonword for item in items):
        raise HTTPException(
            status_code=503,
            detail=(
                "The frequency list is empty, so there are no words to check. "
                "Run: uv run python scripts/import_freq.py"
            ),
        )

    band_count = len({item.band_index for item in items if item.band_index is not None})
    return _templates(request).TemplateResponse(
        request,
        "placement.html",
        {
            "items": items,
            "seed": seed,
            "band_count": band_count,
            "disclaimer": DISCLAIMER,
            "latest": latest_run(session),
        },
    )


@router.post("/placement")
async def placement_submit(request: Request, session: Session = Depends(get_session)):
    form = await request.form()

    raw_seed = form.get("seed")
    if not isinstance(raw_seed, str) or not raw_seed.isdigit():
        raise HTTPException(status_code=422, detail="Missing or invalid seed.")
    seed = int(raw_seed)

    # Rebuild the exact item set from the seed rather than trusting the client
    # to send the words back: the answers then line up with the stored items.
    items = select_items(session, seed)
    if not any(not item.is_nonword for item in items):
        raise HTTPException(status_code=503, detail="The frequency list is empty.")

    answers: list[bool | None] = []
    for index in range(len(items)):
        value = form.get(f"item{index}")
        answers.append(True if value == "yes" else False if value == "no" else None)

    profile = score(items, answers)
    run = save_run(session, seed=seed, items=items, answers=answers, profile=profile)
    return RedirectResponse(url=f"/placement/{run.id}", status_code=303)


@router.get("/placement/latest")
def placement_latest(session: Session = Depends(get_session)):
    run = latest_run(session)
    if run is None:
        return RedirectResponse(url="/placement", status_code=303)
    return RedirectResponse(url=f"/placement/{run.id}", status_code=303)


@router.get("/placement/{run_id}")
def placement_result(run_id: int, request: Request, session: Session = Depends(get_session)):
    run = session.get(PlacementRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Placement run not found")

    responses: list[PlacementResponse] = responses_for(session, run.id)

    # Score the stored responses again for display: one scoring path, and the
    # page cannot drift from the persisted summary.
    items = [
        Item(word=response.word, band_index=response.band_index, is_nonword=response.is_nonword)
        for response in responses
    ]
    answers = [response.claimed_known for response in responses]
    profile = score(items, answers)

    return _templates(request).TemplateResponse(
        request,
        "placement_result.html",
        {"run": run, "profile": profile, "by_band": _answers_by_band(responses)},
    )


def _answers_by_band(responses: list[PlacementResponse]) -> dict[int, list[tuple[str, bool | None]]]:
    """Band index -> [(word, answer)] in display order, for the detail table."""
    grouped: dict[int, list[tuple[str, bool | None]]] = {}
    for response in responses:
        if response.band_index is None:
            continue
        grouped.setdefault(response.band_index, []).append((response.word, response.claimed_known))
    return grouped
