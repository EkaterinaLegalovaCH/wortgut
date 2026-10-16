"""Tests for the lookup endpoint and the save-word flow."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import UserWord

TEXT = "Der Wolf lief durch Häuser und Mädchen."  # every noun/verb sits in the sample dictionary


def make_text(client: TestClient) -> int:
    """Paste TEXT through the real form endpoint and return the new text id."""
    response = client.post(
        "/texts",
        data={"title": "Testtext", "text": TEXT, "source_note": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[-1])


def position_of(client: TestClient, text_id: int, surface: str) -> int:
    """Find a token position by surface form, via the reader page markup."""
    page = client.get(f"/texts/{text_id}").text
    for chunk in page.split('data-position="')[1:]:
        position = chunk.split('"')[0]
        if f">{surface}</button>" in chunk:
            return int(position)
    raise AssertionError(f"token {surface!r} not found on the reader page")


def test_lookup_returns_lemma_translation_and_rank(client_with_dict: TestClient):
    text_id = make_text(client_with_dict)
    position = position_of(client_with_dict, text_id, "lief")

    payload = client_with_dict.get(
        "/api/lookup", params={"text_id": text_id, "position": position}
    ).json()

    assert payload["surface"] == "lief"
    assert payload["lemma"] == "laufen"  # lemmatised, not the surface form
    assert payload["translations"] == ["to run; to walk"]
    assert payload["frequency_rank"] == 700
    assert payload["already_saved"] is False


def test_lookup_uses_lowercased_form_for_frequency_and_capitalised_for_dictionary(
    client_with_dict: TestClient,
):
    """German nouns are capitalised in text but listed lowercase in the frequency file."""
    text_id = make_text(client_with_dict)
    position = position_of(client_with_dict, text_id, "Häuser")

    payload = client_with_dict.get(
        "/api/lookup", params={"text_id": text_id, "position": position}
    ).json()

    assert payload["lemma"] == "Haus"
    assert payload["translations"] == ["house"]
    assert payload["frequency_rank"] == 120  # found via the lowercase form "haus"


def test_lookup_reports_missing_dictionary_entry(client_with_dict: TestClient):
    text_id = make_text(client_with_dict)
    position = position_of(client_with_dict, text_id, "durch")  # not in the sample dictionary

    payload = client_with_dict.get(
        "/api/lookup", params={"text_id": text_id, "position": position}
    ).json()

    assert payload["translations"] == []
    assert payload["frequency_rank"] is None


def test_lookup_unknown_token_is_404(client_with_dict: TestClient):
    response = client_with_dict.get("/api/lookup", params={"text_id": 999, "position": 0})
    assert response.status_code == 404


def test_save_word_stores_context_sentence(client_with_dict: TestClient):
    text_id = make_text(client_with_dict)
    position = position_of(client_with_dict, text_id, "Wolf")

    response = client_with_dict.post(
        "/api/words",
        json={"lemma": "Wolf", "translation": "wolf", "text_id": text_id, "position": position},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["lemma"] == "Wolf"
    assert body["translation"] == "wolf"
    assert body["context_sentence"] == TEXT  # single sentence, stored verbatim
    assert body["encounters"] == 1


def test_saving_the_same_lemma_twice_counts_an_encounter(
    client_with_dict: TestClient, session: Session
):
    text_id = make_text(client_with_dict)
    position = position_of(client_with_dict, text_id, "Wolf")

    for _ in range(2):
        client_with_dict.post(
            "/api/words",
            json={"lemma": "Wolf", "translation": "wolf", "text_id": text_id, "position": position},
        )

    words = session.query(UserWord).filter_by(lemma="Wolf").all()
    assert len(words) == 1  # one row per lemma
    assert words[0].encounters == 2


def test_save_word_without_dictionary_entry_works(client_with_dict: TestClient):
    """The manual-translation path: no entry, user types their own translation."""
    text_id = make_text(client_with_dict)
    position = position_of(client_with_dict, text_id, "durch")

    response = client_with_dict.post(
        "/api/words",
        json={
            "lemma": "durch",
            "translation": "through",
            "text_id": text_id,
            "position": position,
        },
    )

    assert response.status_code == 201
    assert response.json()["translation"] == "through"


def test_save_word_rejects_unknown_text(client_with_dict: TestClient):
    response = client_with_dict.post(
        "/api/words",
        json={"lemma": "Wolf", "translation": "wolf", "text_id": 4242, "position": 0},
    )
    assert response.status_code == 404


def test_lookup_marks_already_saved_words(client_with_dict: TestClient):
    text_id = make_text(client_with_dict)
    position = position_of(client_with_dict, text_id, "Wolf")
    client_with_dict.post(
        "/api/words",
        json={"lemma": "Wolf", "translation": "wolf", "text_id": text_id, "position": position},
    )

    payload = client_with_dict.get(
        "/api/lookup", params={"text_id": text_id, "position": position}
    ).json()

    assert payload["already_saved"] is True
    assert payload["saved_translation"] == "wolf"
