"""PATCH /leads/{lead_id} status-workflow tests (Milestone 5).

Same fixtures as the other suites: the app under test points at
TEST_DATABASE_URL and the leads table is truncated before every test.
"""

import psycopg
import pytest

VALID_PAYLOAD = {
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "company": "Analytical Engines Ltd",
    "message": "Please contact me about a demo.",
    "source": "website-contact-form",
    "external_id": "ext-5001",
}


def create_lead(client, **overrides) -> dict:
    payload = {**VALID_PAYLOAD, **overrides}
    response = client.post("/leads", json=payload)
    assert response.status_code == 201
    return response.json()


def test_valid_transition_new_to_contacted(client) -> None:
    created = create_lead(client)

    response = client.patch(
        f"/leads/{created['lead_id']}", json={"status": "contacted"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "contacted"
    assert body["lead_id"] == created["lead_id"]
    # the update must not touch scoring fields
    assert body["score"] == created["score"]
    assert body["scored_at"] == created["scored_at"]


@pytest.mark.parametrize("value", ["contacted", "qualified", "closed", "new"])
def test_each_vocabulary_value_accepted(client, value: str) -> None:
    created = create_lead(client)

    response = client.patch(f"/leads/{created['lead_id']}", json={"status": value})

    assert response.status_code == 200
    assert response.json()["status"] == value


def test_unknown_status_rejected(client) -> None:
    created = create_lead(client)

    response = client.patch(f"/leads/{created['lead_id']}", json={"status": "banana"})

    assert response.status_code == 422


def test_missing_status_rejected(client) -> None:
    created = create_lead(client)

    response = client.patch(f"/leads/{created['lead_id']}", json={})

    assert response.status_code == 422


def test_whitespace_status_rejected(client) -> None:
    created = create_lead(client)

    response = client.patch(f"/leads/{created['lead_id']}", json={"status": "   "})

    assert response.status_code == 422


def test_non_integer_lead_id_rejected(client) -> None:
    response = client.patch("/leads/not-a-number", json={"status": "contacted"})

    assert response.status_code == 422


def test_unknown_lead_returns_404(client) -> None:
    response = client.patch("/leads/999999", json={"status": "contacted"})

    assert response.status_code == 404
    assert response.json()["detail"] == "Lead not found"


def test_status_is_persisted(client, schema_ready: str) -> None:
    created = create_lead(client)

    response = client.patch(
        f"/leads/{created['lead_id']}", json={"status": "qualified"}
    )
    assert response.status_code == 200

    with psycopg.connect(schema_ready, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM leads WHERE id = %s", (created["lead_id"],)
        ).fetchone()
    assert row == ("qualified",)


def test_same_status_patch_is_idempotent(client) -> None:
    created = create_lead(client)

    first = client.patch(f"/leads/{created['lead_id']}", json={"status": "contacted"})
    second = client.patch(f"/leads/{created['lead_id']}", json={"status": "contacted"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "contacted"


def test_status_filter_reflects_update(client) -> None:
    created = create_lead(client)
    updated = client.patch(
        f"/leads/{created['lead_id']}", json={"status": "contacted"}
    )
    assert updated.status_code == 200

    contacted = client.get("/leads", params={"status": "contacted"})
    still_new = client.get("/leads", params={"status": "new"})

    assert contacted.status_code == still_new.status_code == 200
    assert [item["lead_id"] for item in contacted.json()["items"]] == [
        created["lead_id"]
    ]
    assert still_new.json()["items"] == []


def test_db_check_rejects_invalid_status(client, schema_ready: str) -> None:
    created = create_lead(client)
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE leads SET status = %s WHERE id = %s",
                ("banana", created["lead_id"]),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO leads (name, email, company, message, source, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    VALID_PAYLOAD["name"],
                    VALID_PAYLOAD["email"],
                    VALID_PAYLOAD["company"],
                    VALID_PAYLOAD["message"],
                    VALID_PAYLOAD["source"],
                    "banana",
                ),
            )
