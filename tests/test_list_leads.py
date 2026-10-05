"""GET /leads and GET /leads/{lead_id} tests.

Uses the same fixtures as test_create_lead.py: the app under test points at
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
    "external_id": "ext-2001",
}


def create_lead(client, **overrides) -> dict:
    payload = {**VALID_PAYLOAD, **overrides}
    response = client.post("/leads", json=payload)
    assert response.status_code == 201
    return response.json()


def test_empty_list(client) -> None:
    response = client.get("/leads")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 20
    assert body["offset"] == 0


def test_list_contains_created_lead(client) -> None:
    created = create_lead(client)

    response = client.get("/leads")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["lead_id"] == created["lead_id"]
    assert item["name"] == VALID_PAYLOAD["name"]
    assert item["email"] == VALID_PAYLOAD["email"]
    assert item["company"] == VALID_PAYLOAD["company"]
    assert item["source"] == VALID_PAYLOAD["source"]
    assert item["status"] == "new"
    assert item["created_at"]


def test_status_filter(client, schema_ready: str) -> None:
    first = create_lead(client, external_id="ext-status-1")
    create_lead(client, external_id="ext-status-2")
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        conn.execute("UPDATE leads SET status = 'contacted' WHERE id = %s", (first["lead_id"],))

    new_only = client.get("/leads", params={"status": "new"})
    assert new_only.status_code == 200
    assert new_only.json()["total"] == 1
    assert [item["status"] for item in new_only.json()["items"]] == ["new"]

    contacted = client.get("/leads", params={"status": "contacted"})
    assert contacted.status_code == 200
    assert contacted.json()["total"] == 1
    assert contacted.json()["items"][0]["lead_id"] == first["lead_id"]

    unknown = client.get("/leads", params={"status": "does-not-exist"})
    assert unknown.status_code == 200
    assert unknown.json()["items"] == []
    assert unknown.json()["total"] == 0


def test_source_filter(client) -> None:
    create_lead(client, external_id="ext-src-a1", source="source-a")
    create_lead(client, external_id="ext-src-a2", source="source-a")
    create_lead(client, external_id="ext-src-b1", source="source-b")

    filtered = client.get("/leads", params={"source": "source-a"})
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 2
    assert {item["source"] for item in filtered.json()["items"]} == {"source-a"}

    unknown = client.get("/leads", params={"source": "no-such-source"})
    assert unknown.status_code == 200
    assert unknown.json()["items"] == []
    assert unknown.json()["total"] == 0


def test_pagination(client) -> None:
    for i in range(5):
        create_lead(client, external_id=f"ext-page-{i}")

    page1 = client.get("/leads", params={"limit": 2, "offset": 0})
    page2 = client.get("/leads", params={"limit": 2, "offset": 2})
    page3 = client.get("/leads", params={"limit": 2, "offset": 4})

    assert page1.status_code == page2.status_code == page3.status_code == 200
    ids1 = [item["lead_id"] for item in page1.json()["items"]]
    ids2 = [item["lead_id"] for item in page2.json()["items"]]
    ids3 = [item["lead_id"] for item in page3.json()["items"]]

    assert len(ids1) == len(ids2) == 2
    assert len(ids3) == 1
    assert set(ids1).isdisjoint(ids2)
    assert set(ids1 + ids2).isdisjoint(ids3)
    # total is unaffected by paging
    assert page1.json()["total"] == page2.json()["total"] == page3.json()["total"] == 5


def test_default_ordering_newest_first(client) -> None:
    ids = [create_lead(client, external_id=f"ext-order-{i}")["lead_id"] for i in range(3)]

    response = client.get("/leads")

    assert response.status_code == 200
    listed = [item["lead_id"] for item in response.json()["items"]]
    assert listed == sorted(ids, reverse=True)


def test_get_lead_by_id(client) -> None:
    created = create_lead(client)

    response = client.get(f"/leads/{created['lead_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["lead_id"] == created["lead_id"]
    assert body["name"] == VALID_PAYLOAD["name"]
    assert body["email"] == VALID_PAYLOAD["email"]
    assert body["company"] == VALID_PAYLOAD["company"]
    assert body["source"] == VALID_PAYLOAD["source"]
    assert body["status"] == "new"
    assert body["created_at"]


def test_get_missing_lead_returns_404(client) -> None:
    response = client.get("/leads/999999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Lead not found"


def test_status_query_param_name_is_stable(client) -> None:
    """The wire-level parameter stays `status` on both filtered endpoints.

    The route functions use a local name (`status_filter`) that must not
    leak into the public API contract.
    """
    schema = client.get("/openapi.json").json()

    list_params = {
        param["name"]
        for param in schema["paths"]["/leads"]["get"].get("parameters", [])
    }
    export_params = {
        param["name"]
        for param in schema["paths"]["/leads/export"]["get"].get("parameters", [])
    }

    assert "status" in list_params
    assert "status" in export_params
    assert "status_filter" not in list_params
    assert "status_filter" not in export_params


def test_get_non_integer_lead_id_returns_422(client) -> None:
    response = client.get("/leads/not-a-number")

    assert response.status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_invalid_pagination_returns_422(client, query: str) -> None:
    response = client.get(f"/leads?{query}")

    assert response.status_code == 422
