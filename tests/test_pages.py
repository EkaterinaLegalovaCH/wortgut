"""Smoke tests for every page — these catch template and wiring breakage fast."""

import re

from fastapi.testclient import TestClient

from app.routes.pages import TOKENS_PER_PAGE

_PAGE_COUNTER = re.compile(r"Page \d+ of (\d+)")

SAMPLE_TEXT = "Der Wolf lief durch Häuser und Mädchen."

# Long enough to need several reader pages, short enough to tokenize quickly.
LONG_TEXT = "Der Wolf lief durch den dunklen Wald und suchte nach Beute. " * 600


def test_home_page_loads(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "Paste a text" in response.text
    assert 'action="/texts"' in response.text


def test_home_page_preserves_utf8(client: TestClient):
    """Umlauts in a stored text must round-trip through the list."""
    client.post("/texts", data={"title": "Grüße", "text": SAMPLE_TEXT}, follow_redirects=False)
    response = client.get("/")
    assert "Grüße" in response.text


def test_create_text_redirects_to_reader(client: TestClient):
    response = client.post(
        "/texts", data={"title": "Testtext", "text": SAMPLE_TEXT}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/texts/")


def test_reader_renders_clickable_words(client: TestClient):
    client.post("/texts", data={"title": "Testtext", "text": SAMPLE_TEXT}, follow_redirects=False)
    response = client.get("/texts/1")

    assert response.status_code == 200
    assert 'class="word' in response.text
    assert "Wolf" in response.text
    assert "Mädchen" in response.text  # umlauts survive the round trip
    assert "data-position=" in response.text


def test_reader_marks_saved_words(client_with_dict: TestClient):
    client_with_dict.post(
        "/texts", data={"title": "Testtext", "text": SAMPLE_TEXT}, follow_redirects=False
    )
    client_with_dict.post(
        "/api/words",
        json={"lemma": "Wolf", "translation": "wolf", "text_id": 1, "position": 1},
    )

    page = client_with_dict.get("/texts/1").text
    assert 'class="word saved"' in page


def test_reader_404_for_unknown_text(client: TestClient):
    assert client.get("/texts/999").status_code == 404


def test_empty_text_is_rejected(client: TestClient):
    response = client.post("/texts", data={"title": "leer", "text": "   "})
    assert response.status_code == 422


def test_words_page_is_empty_before_anything_is_saved(client: TestClient):
    response = client.get("/words")
    assert response.status_code == 200
    assert "Nothing saved yet" in response.text


def test_words_page_lists_and_deletes(client_with_dict: TestClient):
    client_with_dict.post(
        "/texts", data={"title": "Testtext", "text": SAMPLE_TEXT}, follow_redirects=False
    )
    client_with_dict.post(
        "/api/words",
        json={"lemma": "Wolf", "translation": "wolf", "text_id": 1, "position": 1},
    )

    page = client_with_dict.get("/words").text
    assert "Wolf" in page
    assert "wolf" in page
    assert "Der Wolf lief durch Häuser und Mädchen." in page  # context sentence shown

    delete = client_with_dict.post("/words/1/delete", follow_redirects=False)
    assert delete.status_code == 303
    assert "Nothing saved yet" in client_with_dict.get("/words").text


def test_health_is_cheap_and_always_ok(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}


def test_ready_reports_each_dependency(client_with_dict: TestClient):
    payload = client_with_dict.get("/ready").json()

    assert set(payload["checks"]) == {"database_reachable", "dictionary_loaded", "spacy_model_loaded"}
    assert payload["checks"]["database_reachable"] is True
    assert payload["checks"]["dictionary_loaded"] is True  # seeded sample dictionary
    assert payload["missing"] == []


def test_ready_reports_missing_dictionary(client: TestClient):
    payload = client.get("/ready").json()

    assert payload["ready"] is False
    assert payload["checks"]["dictionary_loaded"] is False
    assert "dictionary_loaded" in payload["missing"]


def test_static_assets_are_served_locally(client: TestClient):
    """No CDN at runtime: CSS and JS must come from the app itself."""
    css = client.get("/static/style.css")
    js = client.get("/static/reader.js")

    assert css.status_code == 200 and "--reading-size" in css.text
    assert js.status_code == 200 and "api/lookup" in js.text


HINT = "new = not yet confirmed as known"


def test_reader_explains_what_new_means(client: TestClient):
    """Decision 5 §2: the reader hint says what "new" counts."""
    client.post("/texts", data={"title": "Testtext", "text": SAMPLE_TEXT}, follow_redirects=False)
    page = client.get("/texts/1").text

    assert HINT in page


def test_library_explains_what_new_means(client: TestClient):
    """The same wording appears wherever the library lists coverage."""
    client.post("/texts", data={"title": "Testtext", "text": SAMPLE_TEXT}, follow_redirects=False)
    page = client.get("/").text

    assert HINT in page


def _create_long_text(client: TestClient) -> str:
    """Create a text long enough for several pages and return its reader URL."""
    response = client.post(
        "/texts", data={"title": "Langer Text", "text": LONG_TEXT}, follow_redirects=False
    )
    return response.headers["location"]


def _total_pages(html: str) -> int:
    """Read the page count from the pager, failing loudly when it is absent."""
    match = _PAGE_COUNTER.search(html)
    assert match is not None, "the pager is missing from the page"
    return int(match.group(1))


def test_short_text_has_no_pager(client: TestClient):
    """A text that fits on one page needs no navigation."""
    client.post("/texts", data={"title": "Testtext", "text": SAMPLE_TEXT}, follow_redirects=False)
    page = client.get("/texts/1").text

    assert "reader-pager" not in page


def test_long_text_is_split_into_pages(client: TestClient):
    """W-21: a long text renders one page at a time, with a way forward."""
    url = _create_long_text(client)
    page = client.get(url).text

    assert "reader-pager" in page
    assert "Page 1 of" in page
    assert 'href="/texts/1?page=2"' in page
    assert "pager-link disabled" in page  # no Previous on the first page


def test_second_page_links_both_ways(client: TestClient):
    url = _create_long_text(client)
    page = client.get(f"{url}?page=2").text

    assert "Page 2 of" in page
    assert 'href="/texts/1?page=1"' in page
    assert 'href="/texts/1?page=3"' in page


def test_last_page_has_no_next_link(client: TestClient):
    url = _create_long_text(client)
    first = client.get(url).text
    total_pages = _total_pages(first)
    assert total_pages > 1

    last = client.get(f"{url}?page={total_pages}").text

    assert f"Page {total_pages} of {total_pages}" in last
    assert f'href="/texts/1?page={total_pages + 1}"' not in last
    assert "pager-link disabled" in last  # no Next on the last page


def test_out_of_range_page_clamps_to_the_last_page(client: TestClient):
    url = _create_long_text(client)
    first = client.get(url).text
    total_pages = _total_pages(first)

    past_end = client.get(f"{url}?page={total_pages + 500}")
    assert past_end.status_code == 200
    assert f"Page {total_pages} of {total_pages}" in past_end.text

    before_start = client.get(f"{url}?page=0")
    assert before_start.status_code == 200
    assert "Page 1 of" in before_start.text


def test_one_page_stays_small(client: TestClient):
    """W-21's acceptance: a long text's page stays under ~2 MB."""
    url = _create_long_text(client)
    page = client.get(url)

    assert page.status_code == 200
    assert len(page.content) < 2_000_000
    assert page.text.count('class="word') <= TOKENS_PER_PAGE
