"""GET /leads/export and DELETE /leads/{lead_id} tests (Milestone 6).

Same fixtures as the other suites: the app under test points at
TEST_DATABASE_URL and the leads table is truncated before every test.
"""

import csv
import io

VALID_PAYLOAD = {
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "company": "Analytical Engines Ltd",
    "message": "Please contact me about a demo.",
    "source": "website-contact-form",
    "external_id": "ext-6001",
}

EXPECTED_HEADER = [
    "id",
    "name",
    "email",
    "company",
    "message",
    "source",
    "external_id",
    "status",
    "score",
    "score_reason",
    "scored_at",
    "created_at",
]


def create_lead(client, **overrides) -> dict:
    payload = {**VALID_PAYLOAD, **overrides}
    response = client.post("/leads", json=payload)
    assert response.status_code == 201
    return response.json()


def read_csv(response) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.reader(io.StringIO(response.text))
    rows = list(reader)
    header = rows[0]
    records = [dict(zip(header, row)) for row in rows[1:]]
    return header, records


def test_empty_export_has_header_only(client) -> None:
    response = client.get("/leads/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="leads-export.csv"'
    )
    header, records = read_csv(response)
    assert header == EXPECTED_HEADER
    assert records == []


def test_export_contains_created_lead(client) -> None:
    created = create_lead(client)

    response = client.get("/leads/export")

    assert response.status_code == 200
    header, records = read_csv(response)
    assert header == EXPECTED_HEADER
    assert len(records) == 1
    record = records[0]
    assert record["id"] == str(created["lead_id"])
    assert record["name"] == VALID_PAYLOAD["name"]
    assert record["email"] == VALID_PAYLOAD["email"]
    assert record["company"] == VALID_PAYLOAD["company"]
    assert record["message"] == VALID_PAYLOAD["message"]
    assert record["source"] == VALID_PAYLOAD["source"]
    assert record["external_id"] == VALID_PAYLOAD["external_id"]
    assert record["status"] == "new"
    assert record["score"] == str(created["score"])
    assert record["score_reason"] == created["score_reason"]
    assert record["scored_at"]
    assert record["created_at"]


def test_csv_escaping_round_trip(client) -> None:
    tricky = 'He said "send pricing, ASAP",\nthen left. Commas: a, b, c.'
    create_lead(client, message=tricky)

    response = client.get("/leads/export")

    assert response.status_code == 200
    _, records = read_csv(response)
    assert len(records) == 1
    assert records[0]["message"] == tricky


def test_export_filters(client) -> None:
    first = create_lead(client, source="alpha-form", external_id="ext-6101")
    create_lead(client, source="beta-form", external_id="ext-6102")
    assert (
        client.patch(
            f"/leads/{first['lead_id']}", json={"status": "contacted"}
        ).status_code
        == 200
    )

    by_source = client.get("/leads/export", params={"source": "alpha-form"})
    by_status_new = client.get("/leads/export", params={"status": "new"})
    by_unknown = client.get("/leads/export", params={"status": "does-not-exist"})

    _, source_records = read_csv(by_source)
    assert [r["source"] for r in source_records] == ["alpha-form"]

    _, new_records = read_csv(by_status_new)
    assert len(new_records) == 1
    assert new_records[0]["source"] == "beta-form"

    header, unknown_records = read_csv(by_unknown)
    assert header == EXPECTED_HEADER  # header still present when empty
    assert unknown_records == []


def test_export_ordering_newest_first(client) -> None:
    ids = [create_lead(client, external_id=f"ext-620{i}")["lead_id"] for i in range(3)]

    response = client.get("/leads/export")

    assert response.status_code == 200
    _, records = read_csv(response)
    assert [int(r["id"]) for r in records] == sorted(ids, reverse=True)


def test_delete_existing_lead(client) -> None:
    created = create_lead(client)

    delete = client.delete(f"/leads/{created['lead_id']}")
    assert delete.status_code == 204
    assert delete.text == ""

    assert client.get(f"/leads/{created['lead_id']}").status_code == 404
    listing = client.get("/leads")
    assert listing.json()["total"] == 0


def test_delete_unknown_lead_returns_404(client) -> None:
    response = client.delete("/leads/999999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Lead not found"


def test_delete_non_integer_id_returns_422(client) -> None:
    response = client.delete("/leads/not-a-number")

    assert response.status_code == 422


def test_double_delete_second_is_404(client) -> None:
    created = create_lead(client)

    first = client.delete(f"/leads/{created['lead_id']}")
    second = client.delete(f"/leads/{created['lead_id']}")

    assert first.status_code == 204
    assert second.status_code == 404


def test_recreate_after_delete(client) -> None:
    created = create_lead(client)
    assert client.delete(f"/leads/{created['lead_id']}").status_code == 204

    recreated = client.post("/leads", json=VALID_PAYLOAD)

    assert recreated.status_code == 201
    assert recreated.json()["lead_id"] != created["lead_id"]
