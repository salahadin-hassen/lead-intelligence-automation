"""Public AI-trigger abuse protection.

Threat: the lead webhook is public (and n8n is reachable directly, bypassing
the website), every new lead (unique external_id) triggers one paid OpenAI
request, and `demo_insights` is only a client-supplied flag. The control is a
global hourly/daily call budget reserved atomically with the per-lead claim in
POST /leads/{id}/insights, the only code path that spends OpenAI money.

No real OpenAI request is made: httpx.post in app.insights is replaced by a
guard and every test installs a fake generator.
"""

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from app import db, insights
from app.config import Settings, get_settings
from app.main import app
from app.models import AiInsights
from app.routes.leads import get_insights_generator

REPO_ROOT = Path(__file__).resolve().parent.parent
INTAKE_PATH = REPO_ROOT / "n8n" / "lead-intake.json"
PAGE_PATH = REPO_ROOT / "demo" / "lead-form.html"

VALID = {
    "summary": "Synthetic request for a pilot.",
    "intent_category": "consultation",
    "urgency": "low",
    "recommended_next_action": "Offer a discovery call.",
    "reason": "Asks about a pilot without a deadline.",
}


def _lead(i: int, **overrides) -> dict:
    return {
        "name": "Abuse Test",
        "email": "abuse.test@example.com",
        "company": "Example Co",
        "message": "Please send pricing for a pilot.",
        "source": "website-contact-form",
        "external_id": f"abuse-{i:04d}",
        **overrides,
    }


@pytest.fixture(autouse=True)
def no_real_openai(monkeypatch):
    def guard(*args, **kwargs):
        raise AssertionError("tests must never call the real OpenAI API")

    monkeypatch.setattr(insights.httpx, "post", guard)


@pytest.fixture
def ai_settings(monkeypatch):
    def configure(hourly: int = 20, daily: int = 100, enabled: bool = True):
        monkeypatch.setenv("AI_INSIGHTS_ENABLED", "true" if enabled else "false")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
        monkeypatch.setenv("LLM_API_KEY", "")
        monkeypatch.setenv("AI_INSIGHTS_MAX_CALLS_PER_HOUR", str(hourly))
        monkeypatch.setenv("AI_INSIGHTS_MAX_CALLS_PER_DAY", str(daily))
        get_settings.cache_clear()
        return get_settings()

    yield configure
    get_settings.cache_clear()


@pytest.fixture
def generator_calls():
    calls = []

    def fake(lead, settings):
        calls.append(lead["id"])
        return insights.InsightsOutcome(insights=AiInsights.model_validate(VALID), error=None, model="fake-model")

    app.dependency_overrides[get_insights_generator] = lambda: fake
    yield calls
    app.dependency_overrides.pop(get_insights_generator, None)


def _create(client, i: int, **overrides) -> dict:
    res = client.post("/leads", json=_lead(i, **overrides))
    assert res.status_code == 201, res.text
    return res.json()


def _budget(schema_ready: str) -> dict:
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        return dict(conn.execute("SELECT window_kind, calls FROM ai_insights_budget").fetchall())


def _ai_status(schema_ready: str, lead_id: int):
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        return conn.execute("SELECT ai_status, ai_error, score FROM leads WHERE id = %s", (lead_id,)).fetchone()


# -----------------------------------------------------------------------------
# 1-3. Normal intake, permitted demo request, exceeding the policy
# -----------------------------------------------------------------------------


def test_lead_creation_without_ai_request_never_uses_the_budget(client, ai_settings, generator_calls, schema_ready):
    ai_settings()
    lead = _create(client, 1)

    assert lead["score"] is not None
    assert generator_calls == []
    assert _budget(schema_ready) == {}


def test_permitted_request_gets_insights_and_counts_once(client, ai_settings, generator_calls, schema_ready):
    ai_settings(hourly=5, daily=10)
    lead = _create(client, 1)

    body = client.post(f"/leads/{lead['lead_id']}/insights").json()

    assert body["status"] == "completed" and body["insights"] == VALID
    assert generator_calls == [lead["lead_id"]]
    assert _budget(schema_ready) == {"hour": 1, "day": 1}


def test_requests_beyond_the_hourly_limit_make_no_model_call(client, ai_settings, generator_calls, schema_ready):
    ai_settings(hourly=3, daily=100)
    leads = [_create(client, i) for i in range(6)]

    results = [client.post(f"/leads/{lead['lead_id']}/insights").json() for lead in leads]

    assert [r["status"] for r in results] == ["completed"] * 3 + ["failed"] * 3
    assert all(r["error"] == "budget_exceeded" and r["insights"] is None for r in results[3:])
    assert len(generator_calls) == 3
    assert _budget(schema_ready) == {"hour": 3, "day": 3}
    for lead in leads[3:]:
        assert _ai_status(schema_ready, lead["lead_id"]) == (None, None, lead["score"]), "lead untouched, still claimable"
        assert client.get(f"/leads/{lead['lead_id']}/insights").json()["status"] == "not_requested"


def test_daily_limit_is_enforced_independently(client, ai_settings, generator_calls, schema_ready):
    ai_settings(hourly=100, daily=2)
    leads = [_create(client, i) for i in range(4)]

    statuses = [client.post(f"/leads/{lead['lead_id']}/insights").json()["status"] for lead in leads]

    assert statuses == ["completed", "completed", "failed", "failed"]
    assert len(generator_calls) == 2
    assert _budget(schema_ready) == {"hour": 2, "day": 2}, "a denied day window must not consume the hour window"


@pytest.mark.parametrize("hourly,daily", [(0, 100), (100, 0), (-1, 100)])
def test_a_zero_or_negative_limit_disables_model_calls(client, ai_settings, generator_calls, hourly, daily):
    ai_settings(hourly=hourly, daily=daily)
    lead = _create(client, 1)

    assert client.post(f"/leads/{lead['lead_id']}/insights").json()["error"] == "budget_exceeded"
    assert generator_calls == []


def test_a_deferred_lead_is_enriched_when_a_new_window_opens(client, ai_settings, generator_calls, schema_ready):
    ai_settings(hourly=1, daily=100)
    first, second = _create(client, 1), _create(client, 2)
    client.post(f"/leads/{first['lead_id']}/insights")
    assert client.post(f"/leads/{second['lead_id']}/insights").json()["error"] == "budget_exceeded"

    with psycopg.connect(schema_ready, autocommit=True) as conn:  # the hour passes
        conn.execute("UPDATE ai_insights_budget SET window_start = window_start - interval '1 hour' WHERE window_kind = 'hour'")

    assert client.post(f"/leads/{second['lead_id']}/insights").json()["status"] == "completed"
    assert generator_calls == [first["lead_id"], second["lead_id"]]


@pytest.mark.parametrize("state", ["completed", "pending", "failed", "disabled"])
def test_paths_without_a_model_call_never_consume_budget(client, ai_settings, generator_calls, schema_ready, state):
    ai_settings(hourly=5, daily=5, enabled=state != "disabled")
    lead = _create(client, 1)
    if state in ("completed", "pending", "failed"):
        with psycopg.connect(schema_ready, autocommit=True) as conn:
            conn.execute(
                "UPDATE leads SET ai_status = %s, ai_updated_at = now(), ai_insights = %s WHERE id = %s",
                (state, json.dumps(VALID) if state == "completed" else None, lead["lead_id"]),
            )

    client.post(f"/leads/{lead['lead_id']}/insights")

    assert generator_calls == []
    assert _budget(schema_ready) == {}


def test_default_limits_match_the_documented_cost_estimate():
    """The call cap is enforced; the dollar figure is only an estimate (docs Section 11)."""
    settings = Settings(database_url="unused")
    assert (settings.ai_insights_max_calls_per_hour, settings.ai_insights_max_calls_per_day) == (20, 100)
    # Estimate: ~1,000 input tokens, the 300-token output cap, 2026-10-24 Standard prices.
    estimated_cost_per_call = 1000 * 0.10 / 1e6 + settings.ai_insights_max_output_tokens * 0.50 / 1e6
    assert settings.ai_insights_max_calls_per_day * estimated_cost_per_call < 0.03  # USD per day


# -----------------------------------------------------------------------------
# 4. Direct webhook requests and concurrency
# -----------------------------------------------------------------------------


def test_every_created_lead_reaches_only_the_budgeted_endpoint_whatever_the_flag():
    """The demo flag only selects the response node; it cannot add or skip a paid call."""
    wf = json.loads(INTAKE_PATH.read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in wf["nodes"]}
    http_urls = {n["name"]: n["parameters"]["url"] for n in nodes.values() if n["type"] == "n8n-nodes-base.httpRequest"}
    assert http_urls == {
        "Create Lead in FastAPI": "={{ $json.apiLeadsUrl }}",
        "Generate AI Insights": nodes["Generate AI Insights"]["parameters"]["url"],
    }
    assert "/insights" in http_urls["Generate AI Insights"] and "openai" not in json.dumps(wf).lower()
    created_targets = [e["node"] for e in wf["connections"]["Route on API Status"]["main"][0]]
    assert "Generate AI Insights" in created_targets, "reached unconditionally from the 201 branch"
    assert "Generate AI Insights" not in json.dumps(wf["connections"]["Demo Insights Requested?"])


def test_request_parameters_cannot_bypass_the_budget(client, ai_settings, generator_calls):
    ai_settings(hourly=1, daily=100)
    first, second = _create(client, 1), _create(client, 2)
    client.post(f"/leads/{first['lead_id']}/insights")

    for query in ("?demo_insights=true", "?retry=true", "?retry=true&demo_insights=1"):
        assert client.post(f"/leads/{second['lead_id']}/insights{query}").json()["error"] == "budget_exceeded"
    assert generator_calls == [first["lead_id"]]


def _reserve(lead_id: int, hourly: int, daily: int = 1000) -> str:
    return db.reserve_lead_insights(lead_id, retry=False, stale_seconds=75, hourly_limit=hourly, daily_limit=daily)


@pytest.mark.parametrize("hourly,daily,cap", [(5, 1000, 5), (1000, 4, 4)], ids=["hourly-cap", "daily-cap"])
def test_concurrent_reservations_cannot_exceed_the_cap(client, schema_ready, hourly, daily, cap):
    leads = [_create(client, i)["lead_id"] for i in range(12)]
    barrier = threading.Barrier(len(leads))

    def attempt(lead_id):
        barrier.wait()
        return _reserve(lead_id, hourly=hourly, daily=daily)

    with ThreadPoolExecutor(max_workers=len(leads)) as pool:
        results = list(pool.map(attempt, leads))

    assert results.count("reserved") == cap
    assert results.count("budget_exceeded") == len(leads) - cap
    assert _budget(schema_ready) == {"hour": cap, "day": cap}, "a denied window never keeps the other's increment"


def test_concurrent_reservations_for_one_lead_pay_once(client, schema_ready):
    lead_id = _create(client, 1)["lead_id"]
    barrier = threading.Barrier(6)

    def attempt(_):
        barrier.wait()
        return _reserve(lead_id, hourly=50)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))

    assert results.count("reserved") == 1 and results.count("not_claimable") == 5
    assert _budget(schema_ready) == {"hour": 1, "day": 1}


# -----------------------------------------------------------------------------
# 5. Reading another lead's insights
# -----------------------------------------------------------------------------


def test_public_callers_cannot_call_the_insights_api_in_production(client, ai_settings, monkeypatch):
    ai_settings()
    lead_id = _create(client, 1)["lead_id"]
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("API_AUTH_TOKEN", "server-side-token-not-real")
    get_settings.cache_clear()

    for headers in ({}, {"Authorization": "Bearer guessed"}):
        assert client.get(f"/leads/{lead_id}/insights", headers=headers).status_code == 401
        assert client.post(f"/leads/{lead_id}/insights", headers=headers).status_code == 401


def _run_prepare(body: dict) -> dict:
    code = json.loads(INTAKE_PATH.read_text())["nodes"]
    code = next(n for n in code if n["name"] == "Prepare Lead Payload")["parameters"]["jsCode"]
    script = """
    const [code, body] = [process.argv[1], JSON.parse(process.argv[2])];
    const $input = { first: () => ({ json: { body } }) };
    const $env = { LEAD_API_BASE_URL: "https://api.example.test" };
    console.log(JSON.stringify(new Function("$input", "$env", code)($input, $env)[0].json));
    """
    res = subprocess.run(["node", "-e", script, code, json.dumps(body)], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def test_webhook_body_cannot_choose_which_leads_insights_are_fetched():
    out = _run_prepare({**_lead(1), "lead_id": 1, "ai_insights": {"status": "completed"}, "retry": True})

    assert set(out["lead"]) == {"name", "email", "company", "message", "source", "external_id"}
    url = json.loads(INTAKE_PATH.read_text())["nodes"]
    url = next(n for n in url if n["name"] == "Generate AI Insights")["parameters"]["url"]
    assert "$('Create Lead in FastAPI').first().json.body.lead_id" in url
    assert "Lead Webhook" not in url and "$json" not in url


def test_replaying_another_leads_external_id_returns_no_insights(client, ai_settings, generator_calls, schema_ready):
    ai_settings()
    victim = _create(client, 1)
    client.post(f"/leads/{victim['lead_id']}/insights")

    replay = client.post("/leads", json=_lead(1, name="Attacker"))

    assert replay.status_code == 409
    assert set(replay.json()) == {"detail"}
    wf = json.loads(INTAKE_PATH.read_text())
    duplicate_targets = [e["node"] for e in wf["connections"]["Route on API Status"]["main"][1]]
    assert duplicate_targets == ["Respond Duplicate"]
    assert "ai_insights" not in next(n for n in wf["nodes"] if n["name"] == "Respond Duplicate")["parameters"]["responseBody"]
    assert _budget(schema_ready) == {"hour": 1, "day": 1}


def _attach(created: dict, insights_http: dict) -> dict:
    script = """
    const { attachAiInsights } = require("./n8n/attach-ai-insights.js");
    console.log(JSON.stringify(attachAiInsights(JSON.parse(process.argv[1]), JSON.parse(process.argv[2]))));
    """
    res = subprocess.run(
        ["node", "-e", script, json.dumps(created), json.dumps(insights_http)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=True,
    )
    return json.loads(res.stdout)


# -----------------------------------------------------------------------------
# 6-7. Budget/credit failures and a wrong lead_id never break intake
# -----------------------------------------------------------------------------

CREATED = {"statusCode": 201, "body": {"lead_id": 23, "score": 75, "score_reason": "High-intent keywords"}}


def test_wrong_lead_id_in_ai_response_is_withheld():
    out = _attach(CREATED, {"statusCode": 200, "body": {"lead_id": 22, "status": "completed", "insights": VALID}})
    assert out["aiInsights"] == {"status": "unavailable", "lead_id": 23, "insights": None, "error": "lead_id_mismatch"}


@pytest.mark.parametrize("error", ["budget_exceeded", "insufficient_quota"])
def test_budget_or_credit_exhaustion_keeps_the_created_lead_and_score(error):
    out = _attach(CREATED, {"statusCode": 200, "body": {"lead_id": 23, "status": "failed", "error": error, "insights": None}})

    assert out["statusCode"] == 201 and out["body"] == CREATED["body"]
    assert out["aiInsights"] == {"status": "failed", "lead_id": 23, "insights": None, "error": error}


def test_budget_check_failure_fails_closed_without_a_model_call(client, ai_settings, generator_calls, monkeypatch, schema_ready):
    ai_settings()
    lead = _create(client, 1)

    def broken(*args, **kwargs):
        raise psycopg.OperationalError("budget table unavailable")

    monkeypatch.setattr(db, "reserve_lead_insights", broken)
    with pytest.raises(psycopg.OperationalError):
        client.post(f"/leads/{lead['lead_id']}/insights")

    assert generator_calls == []
    assert _ai_status(schema_ready, lead["lead_id"]) == (None, None, lead["score"])


def test_demo_page_explains_an_exhausted_allowance_without_fake_insights():
    page = PAGE_PATH.read_text(encoding="utf-8")
    block = page[page.index("/* AI_INSIGHTS_VIEW_START"):page.index("/* AI_INSIGHTS_VIEW_END */")]
    script = """
    const [block, data] = [process.argv[1], JSON.parse(process.argv[2])];
    console.log(JSON.stringify(new Function("data", block + "\\nreturn describeAiInsights(data);")(data)));
    """
    data = {"lead": {"lead_id": 23}, "ai_insights": {"status": "failed", "lead_id": 23, "insights": None, "error": "budget_exceeded"}}
    res = subprocess.run(["node", "-e", script, block, json.dumps(data)], capture_output=True, text=True, check=True)
    view = json.loads(res.stdout)

    assert view["state"] == "unavailable" and view["badge"] == "Failed"
    assert "allowance is used up" in view["note"] and "saved and scored" in view["note"]
    assert "fields" not in view


# -----------------------------------------------------------------------------
# 8. Duplicates and notifications are unchanged
# -----------------------------------------------------------------------------


def test_duplicates_consume_no_budget_and_make_no_call(client, ai_settings, generator_calls, schema_ready):
    ai_settings()
    _create(client, 1)

    assert client.post("/leads", json=_lead(1)).status_code == 409
    assert generator_calls == [] and _budget(schema_ready) == {}


def test_budget_exceeded_does_not_change_qualification_or_notification_routing():
    wf = json.loads(INTAKE_PATH.read_text())
    nodes = {n["name"]: n for n in wf["nodes"]}
    attached = _attach(CREATED, {"statusCode": 200, "body": {"lead_id": 23, "status": "failed", "error": "budget_exceeded"}})
    script = """
    const [extract, qualify, input] = [process.argv[1], process.argv[2], JSON.parse(process.argv[3])];
    const run = (code, json, named) => new Function("$input", "$", code)(
      { first: () => ({ json }) }, (n) => ({ first: () => ({ json: named[n] }) }))[0].json;
    const extracted = run(extract, input, {});
    console.log(JSON.stringify(run(qualify, extracted, { "Prepare Lead Payload": { qualifiedThreshold: 70, thresholdSource: "t" } })));
    """
    res = subprocess.run(
        ["node", "-e", script, nodes["Extract Lead + Score"]["parameters"]["jsCode"],
         nodes["Qualification"]["parameters"]["jsCode"], json.dumps(attached)],
        capture_output=True, text=True, check=True,
    )
    qualified = json.loads(res.stdout)
    assert (qualified["leadId"], qualified["score"], qualified["qualification"]) == (23, 75, "qualified")
    priority_targets = sorted(e["node"] for e in wf["connections"]["Log Priority Route"]["main"][0])
    assert priority_targets == ["Build Audit Event", "Build Qualified Notification", "Build Qualified WhatsApp Notification"]
