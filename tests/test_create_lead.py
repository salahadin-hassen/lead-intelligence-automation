"""POST /leads tests: valid input, invalid email, missing fields,
database persistence, and duplicate (source, external_id) behavior.
"""

import psycopg
import pytest

VALID_PAYLOAD = {
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "company": "Analytical Engines Ltd",
    "message": "Please contact me about a demo.",
    "source": "website-contact-form",
    "external_id": "ext-1001",
}


def test_valid_lead_created(client) -> None:
    response = client.post("/leads", json=VALID_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["lead_id"], int)
    assert body["name"] == VALID_PAYLOAD["name"]
    assert body["email"] == VALID_PAYLOAD["email"]
    assert body["company"] == VALID_PAYLOAD["company"]
    assert body["source"] == VALID_PAYLOAD["source"]
    assert body["status"] == "new"
    assert body["created_at"]


@pytest.mark.parametrize(
    "email",
    ["not-an-email", "missing-at.example.com", "missing-domain@", "@no-local-part", ""],
)
def test_invalid_email_rejected(client, email: str) -> None:
    payload = {**VALID_PAYLOAD, "email": email}

    response = client.post("/leads", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["name", "email", "company", "message", "source"])
def test_missing_required_fields(client, field: str) -> None:
    payload = {key: value for key, value in VALID_PAYLOAD.items() if key != field}

    response = client.post("/leads", json=payload)

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["name", "company", "message", "source"])
def test_blank_required_fields_rejected(client, field: str) -> None:
    payload = {**VALID_PAYLOAD, field: "   "}

    response = client.post("/leads", json=payload)

    assert response.status_code == 422


def test_persists_to_database(client, schema_ready: str) -> None:
    response = client.post("/leads", json=VALID_PAYLOAD)
    assert response.status_code == 201
    lead_id = response.json()["lead_id"]

    with psycopg.connect(schema_ready, autocommit=True) as conn:
        row = conn.execute(
            "SELECT name, email, company, message, source, external_id, status "
            "FROM leads WHERE id = %s",
            (lead_id,),
        ).fetchone()

    assert row is not None
    assert row == (
        VALID_PAYLOAD["name"],
        VALID_PAYLOAD["email"],
        VALID_PAYLOAD["company"],
        VALID_PAYLOAD["message"],
        VALID_PAYLOAD["source"],
        VALID_PAYLOAD["external_id"],
        "new",
    )


def test_duplicate_external_id_rejected(client, schema_ready: str) -> None:
    first = client.post("/leads", json=VALID_PAYLOAD)
    assert first.status_code == 201

    second = client.post("/leads", json=VALID_PAYLOAD)

    assert second.status_code == 409
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM leads WHERE source = %s AND external_id = %s",
            (VALID_PAYLOAD["source"], VALID_PAYLOAD["external_id"]),
        ).fetchone()[0]
    assert count == 1


def test_duplicate_different_source_allowed(client, schema_ready: str) -> None:
    first = client.post("/leads", json=VALID_PAYLOAD)
    assert first.status_code == 201

    second = client.post("/leads", json={**VALID_PAYLOAD, "source": "newsletter-signup"})

    assert second.status_code == 201
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM leads WHERE external_id = %s",
            (VALID_PAYLOAD["external_id"],),
        ).fetchone()[0]
    assert count == 2


def test_null_external_id_not_unique(client, schema_ready: str) -> None:
    payload = {**VALID_PAYLOAD, "external_id": None}

    first = client.post("/leads", json=payload)
    second = client.post("/leads", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        count = conn.execute("SELECT count(*) FROM leads").fetchone()[0]
    assert count == 2
