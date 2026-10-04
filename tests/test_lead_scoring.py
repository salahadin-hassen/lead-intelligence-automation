"""Milestone 3: AI lead scoring tests.

Fully offline: the scorer is injected through FastAPI dependency overrides,
so no API key or network access is ever required. Database tests keep the
existing fail-loud TEST_DATABASE_URL rules.
"""

import psycopg
import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.main import app
from app.models import ScoringResult
from app.routes.leads import get_scorer
from app.scoring import parse_scoring_reply

VALID_PAYLOAD = {
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "company": "Analytical Engines Ltd",
    "message": "Please contact me about a demo.",
    "source": "website-contact-form",
    "external_id": "ext-3001",
}


@pytest.fixture
def install_scorer():
    """Replace the route's scorer; returns a call-recording installer."""
    calls = []

    def install(fake):
        calls.clear()

        def recording_scorer(lead):
            calls.append(lead)
            return fake(lead)

        app.dependency_overrides[get_scorer] = lambda: recording_scorer
        return calls

    yield install
    app.dependency_overrides.pop(get_scorer, None)


def test_missing_api_key_skips_scoring(client, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert not get_settings().openai_api_key
        response = client.post("/leads", json=VALID_PAYLOAD)
    finally:
        get_settings.cache_clear()

    assert response.status_code == 201
    body = response.json()
    assert body["score"] is None
    assert body["score_reason"] is None
    assert body["scored_at"] is None


def test_fake_scorer_result_returned(client, install_scorer) -> None:
    calls = install_scorer(
        lambda lead: ScoringResult(score=87, reason="Strong ICP fit.")
    )

    response = client.post("/leads", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["score"] == 87
    assert body["score_reason"] == "Strong ICP fit."
    assert body["scored_at"]
    assert len(calls) == 1
    assert calls[0].name == VALID_PAYLOAD["name"]
    assert calls[0].source == VALID_PAYLOAD["source"]


def test_score_is_persisted_and_visible_in_get_and_list(
    client, install_scorer, schema_ready: str
) -> None:
    install_scorer(lambda lead: ScoringResult(score=64, reason="Decent fit."))
    response = client.post("/leads", json=VALID_PAYLOAD)
    assert response.status_code == 201
    lead_id = response.json()["lead_id"]

    with psycopg.connect(schema_ready, autocommit=True) as conn:
        row = conn.execute(
            "SELECT score, score_reason, scored_at FROM leads WHERE id = %s",
            (lead_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == 64
    assert row[1] == "Decent fit."
    assert row[2] is not None

    by_id = client.get(f"/leads/{lead_id}")
    assert by_id.status_code == 200
    assert by_id.json()["score"] == 64

    in_list = client.get("/leads")
    assert in_list.status_code == 200
    assert in_list.json()["items"][0]["score"] == 64


def test_scorer_exception_still_returns_201(client, install_scorer) -> None:
    def exploding_scorer(lead):
        raise RuntimeError("scorer exploded")

    install_scorer(exploding_scorer)

    response = client.post("/leads", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["score"] is None
    assert body["score_reason"] is None
    assert body["scored_at"] is None


def test_duplicate_never_calls_scorer(client, install_scorer) -> None:
    calls = install_scorer(lambda lead: ScoringResult(score=50, reason="ok"))

    first = client.post("/leads", json=VALID_PAYLOAD)
    second = client.post("/leads", json=VALID_PAYLOAD)

    assert first.status_code == 201
    assert second.status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        '{"score": 101, "reason": "too high"}',
        '{"score": -5, "reason": "negative"}',
        '{"reason": "missing score"}',
        '{"score": 50}',
        '{"score": "high", "reason": "not an integer"}',
        None,
    ],
)
def test_parse_invalid_reply_returns_none(content) -> None:
    assert parse_scoring_reply(content) is None


def test_parse_valid_reply() -> None:
    result = parse_scoring_reply('{"score": 72, "reason": "Good ICP fit."}')

    assert result is not None
    assert result.score == 72
    assert result.reason == "Good ICP fit."


def test_scoring_result_enforces_bounds() -> None:
    with pytest.raises(ValidationError):
        ScoringResult(score=101, reason="out of range")
    with pytest.raises(ValidationError):
        ScoringResult(score=50, reason="")
