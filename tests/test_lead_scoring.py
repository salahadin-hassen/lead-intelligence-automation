"""Milestone 3: AI lead scoring tests.

Fully offline: the scorer is injected through FastAPI dependency overrides,
so no API key or network access is ever required. Database tests keep the
existing fail-loud TEST_DATABASE_URL rules.
"""

import psycopg
import pytest
from pydantic import ValidationError

from app import scoring
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


def test_missing_api_key_uses_heuristic_scorer(client, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert not get_settings().openai_api_key
        response = client.post("/leads", json=VALID_PAYLOAD)
    finally:
        get_settings.cache_clear()

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["score"], int)
    assert 0 <= body["score"] <= 100
    assert body["score_reason"]
    assert body["scored_at"]


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


# --- Milestone 4: offline heuristic scoring ---------------------------------

HIGH_INTENT_MESSAGE = (
    "We are evaluating tools for our enterprise team. Please schedule a "
    "demo and share pricing for integration with our API."
)


class _FakeLLMResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {"message": {"content": '{"score": 77, "reason": "LLM says high fit."}'}}
            ]
        }


def test_high_intent_scores_higher_than_junk(client, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        intent = client.post(
            "/leads", json={**VALID_PAYLOAD, "message": HIGH_INTENT_MESSAGE}
        )
        junk = client.post(
            "/leads",
            json={**VALID_PAYLOAD, "message": "asdf", "external_id": "ext-3002"},
        )
    finally:
        get_settings.cache_clear()

    assert intent.status_code == junk.status_code == 201
    intent_score = intent.json()["score"]
    junk_score = junk.json()["score"]
    assert 0 <= junk_score < intent_score <= 100
    assert intent_score - junk_score >= 30


def test_corporate_domain_scores_higher_than_free_mail(client, monkeypatch) -> None:
    message = "Hello, we would like to learn more about the product."
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        corp = client.post(
            "/leads",
            json={**VALID_PAYLOAD, "message": message, "email": "ada@company.example"},
        )
        free = client.post(
            "/leads",
            json={
                **VALID_PAYLOAD,
                "message": message,
                "email": "ada@gmail.com",
                "external_id": "ext-3003",
            },
        )
    finally:
        get_settings.cache_clear()

    assert corp.status_code == free.status_code == 201
    corp_score = corp.json()["score"]
    free_score = free.json()["score"]
    assert 0 <= free_score < corp_score <= 100
    assert corp_score - free_score == 20


def test_identical_payloads_score_identically(client, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        first = client.post("/leads", json={**VALID_PAYLOAD, "external_id": "ext-3004"})
        second = client.post("/leads", json={**VALID_PAYLOAD, "external_id": "ext-3005"})
    finally:
        get_settings.cache_clear()

    assert first.status_code == second.status_code == 201
    assert first.json()["score"] == second.json()["score"]
    assert first.json()["score_reason"] == second.json()["score_reason"]


def test_heuristic_score_persisted_and_visible(client, schema_ready: str, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = client.post("/leads", json=VALID_PAYLOAD)
    finally:
        get_settings.cache_clear()
    assert response.status_code == 201
    body = response.json()
    lead_id = body["lead_id"]

    with psycopg.connect(schema_ready, autocommit=True) as conn:
        row = conn.execute(
            "SELECT score, score_reason, scored_at FROM leads WHERE id = %s",
            (lead_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == body["score"]
    assert row[1] == body["score_reason"]
    assert row[2] is not None

    assert client.get(f"/leads/{lead_id}").json()["score"] == body["score"]
    assert client.get("/leads").json()["items"][0]["score"] == body["score"]


def test_llm_scorer_wins_when_key_set(client, monkeypatch) -> None:
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _FakeLLMResponse()

    monkeypatch.setattr(scoring.httpx, "post", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy-not-real")
    get_settings.cache_clear()
    try:
        response = client.post("/leads", json=VALID_PAYLOAD)
    finally:
        get_settings.cache_clear()

    assert response.status_code == 201
    body = response.json()
    assert body["score"] == 77
    assert body["score_reason"] == "LLM says high fit."
    assert len(calls) == 1


def test_no_network_without_key(client, monkeypatch) -> None:
    calls = []

    def unexpected_post(*args, **kwargs):
        calls.append(args)
        return _FakeLLMResponse()

    monkeypatch.setattr(scoring.httpx, "post", unexpected_post)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = client.post("/leads", json=VALID_PAYLOAD)
    finally:
        get_settings.cache_clear()

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["score"], int)
    assert calls == []


@pytest.mark.parametrize(
    "message,email,source",
    [
        ("x", "a@gmail.com", "newsletter-weekly"),
        ("a" * 1000, "a@company.example", "referral-program"),
        (
            "demo pricing quote trial pilot enterprise integration api urgent purchase " * 3,
            "a@gmail.com",
            "social-post",
        ),
    ],
)
def test_heuristic_stays_within_bounds(
    client, monkeypatch, message: str, email: str, source: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/leads",
            json={
                **VALID_PAYLOAD,
                "message": message,
                "email": email,
                "source": source,
                "external_id": "ext-3010",
            },
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["score"], int)
    assert 0 <= body["score"] <= 100
    assert 1 <= len(body["score_reason"]) <= 280
