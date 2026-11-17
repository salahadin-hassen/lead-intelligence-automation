"""Server-side Cloudflare Turnstile verification on POST /leads (Task 05A).

Every Cloudflare call is mocked: the real app.turnstile.verify_token runs with
an injected fake `post`, so hostname/action/error-code handling is exercised
for real while no network request is ever made and no real secret is needed.
The leads table lives in the isolated TEST_DATABASE_URL (conftest fixtures).
"""

import json
from functools import partial
from pathlib import Path

import httpx
import psycopg
import pytest

from app import turnstile
from app.config import get_settings
from app.main import app
from app.routes.leads import get_scorer, get_turnstile_verifier
from app.scoring import score_lead_heuristic

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBSITE_WORKFLOWS = [REPO_ROOT / "n8n" / "lead-intake.json", REPO_ROOT / "n8n" / "lead-intake-ai-demo.json"]

SECRET = "turnstile-secret-for-tests-not-real"
HOST = "leadintel.example"
ACTION = "lead-intake"
API_TOKEN = "api-bearer-for-tests-not-real"

LEAD = {
    "name": "Jamie Example",
    "email": "jamie@example.com",
    "company": "Example Systems",
    "message": "We are evaluating a demo and pricing for an enterprise rollout.",
    "source": "website-demo-contact-form",
    "external_id": "ts-0001",
}


class FakeSiteverify:
    """Scripted stand-in for Cloudflare Siteverify, keyed by token prefix."""

    def __init__(self):
        self.calls: list[dict] = []
        self.spent: set[str] = set()

    def __call__(self, url, *, data, timeout):
        self.calls.append({"url": url, "data": dict(data), "timeout": timeout})
        token = data["response"]
        if token.startswith("timeout"):
            raise httpx.ReadTimeout("simulated timeout")
        if token.startswith("connect"):
            raise httpx.ConnectError("simulated connection failure")
        if token.startswith("boom"):
            raise RuntimeError("simulated unexpected failure")
        if token.startswith("http500"):
            return httpx.Response(500, text="upstream error")
        if token.startswith("html"):
            return httpx.Response(200, text="<html>not json</html>")
        if token.startswith("notbool"):
            return httpx.Response(200, json={"success": "true", "hostname": HOST, "action": ACTION})
        if token.startswith("list"):
            return httpx.Response(200, json=[True])
        if token.startswith("provider-"):
            return httpx.Response(200, json={"success": False, "error-codes": [token.removeprefix("provider-")]})
        if token.startswith("invalid"):
            return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]})
        if token.startswith("nocodes"):
            return httpx.Response(200, json={"success": False})
        if token.startswith("wronghost"):
            return httpx.Response(200, json={"success": True, "hostname": "attacker.example", "action": ACTION})
        if token.startswith("nohost"):
            return httpx.Response(200, json={"success": True, "action": ACTION})
        if token.startswith("wrongaction"):
            return httpx.Response(200, json={"success": True, "hostname": HOST, "action": "other-form"})
        if token.startswith("good"):
            if token in self.spent:  # Cloudflare tokens are single-use
                return httpx.Response(200, json={"success": False, "error-codes": ["timeout-or-duplicate"]})
            self.spent.add(token)
            return httpx.Response(200, json={"success": True, "hostname": HOST, "action": ACTION})
        raise AssertionError(f"unscripted token {token!r}")


@pytest.fixture
def turnstile_on(monkeypatch):
    def configure(**env: str):
        values = {
            "TURNSTILE_ENABLED": "true",
            "TURNSTILE_SECRET_KEY": SECRET,
            "TURNSTILE_ALLOWED_HOSTNAMES": f"{HOST}, www.{HOST}",
            "TURNSTILE_EXPECTED_ACTION": ACTION,
            "TURNSTILE_TIMEOUT_SECONDS": "4",
            "LLM_API_KEY": "",
            **env,
        }
        for key, value in values.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_settings()

    configure()
    yield configure
    get_settings.cache_clear()


@pytest.fixture
def siteverify():
    fake = FakeSiteverify()
    app.dependency_overrides[get_turnstile_verifier] = lambda: partial(turnstile.verify_token, post=fake)
    yield fake
    app.dependency_overrides.pop(get_turnstile_verifier, None)


@pytest.fixture
def scorer_calls():
    calls = []

    def recording_scorer(lead):
        calls.append(lead.external_id)
        return score_lead_heuristic(lead)

    app.dependency_overrides[get_scorer] = lambda: recording_scorer
    yield calls
    app.dependency_overrides.pop(get_scorer, None)


def _post(client, *, token=None, channel=None, bearer=None, **overrides):
    headers = {}
    if token is not None:
        headers["X-Turnstile-Token"] = token
    if channel is not None:
        headers["X-Intake-Channel"] = channel
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post("/leads", json={**LEAD, **overrides}, headers=headers)


def _counts(dsn: str) -> tuple[int, int]:
    with psycopg.connect(dsn) as conn:
        leads = conn.execute("SELECT count(*) FROM leads").fetchone()[0]
        budget = conn.execute("SELECT count(*) FROM ai_insights_budget").fetchone()[0]
    return leads, budget


# --- 1: valid token ------------------------------------------------------------


def test_valid_token_permits_lead_creation(client, turnstile_on, siteverify, scorer_calls, schema_ready):
    res = _post(client, token="good-1")
    assert res.status_code == 201, res.text
    assert res.json()["source"] == LEAD["source"]
    assert res.json()["score"] is not None
    assert scorer_calls == [LEAD["external_id"]]
    assert _counts(schema_ready) == (1, 0)


def test_siteverify_request_uses_official_endpoint_secret_and_bounded_timeout(client, turnstile_on, siteverify):
    assert _post(client, token="good-req").status_code == 201
    [call] = siteverify.calls
    assert call["url"] == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    assert call["data"] == {"secret": SECRET, "response": "good-req"}
    assert call["timeout"] == 4.0


# --- 2-6, 14-16: rejections (403) create nothing, score nothing, reserve nothing


@pytest.mark.parametrize(
    "token",
    [
        None,  # 2: missing
        "",  # missing (empty header)
        "   ",  # missing (blank)
        "invalid-1",  # 3: invalid
        "nocodes-1",  # failure without error codes
        "wronghost-1",  # 5: wrong hostname
        "nohost-1",  # hostname absent
        "wrongaction-1",  # 6: wrong action
        "good-" + "x" * turnstile.MAX_TOKEN_LENGTH,  # longer than Cloudflare's limit
    ],
)
def test_rejected_token_returns_403_without_side_effects(
    client, turnstile_on, siteverify, scorer_calls, schema_ready, token
):
    res = _post(client, token=token)
    assert res.status_code == 403
    assert res.json() == {"detail": "Submission verification failed"}
    assert scorer_calls == []  # 15
    assert _counts(schema_ready) == (0, 0)  # 14, 16


def test_missing_and_oversized_tokens_never_reach_cloudflare(client, turnstile_on, siteverify):
    assert _post(client).status_code == 403
    assert _post(client, token="good-" + "x" * turnstile.MAX_TOKEN_LENGTH).status_code == 403
    assert siteverify.calls == []


def test_reused_token_is_rejected(client, turnstile_on, siteverify, scorer_calls, schema_ready):  # 4
    assert _post(client, token="good-once").status_code == 201
    replay = _post(client, token="good-once", external_id="ts-0002")
    assert replay.status_code == 403
    assert scorer_calls == [LEAD["external_id"]]
    assert _counts(schema_ready) == (1, 0)


def test_any_configured_hostname_is_accepted_but_only_exactly(monkeypatch):
    settings_ok = get_settings().model_copy(
        update={"turnstile_secret_key": SECRET, "turnstile_allowed_hostnames": f"{HOST},www.{HOST}"}
    )

    def reply(hostname):
        return lambda url, *, data, timeout: httpx.Response(
            200, json={"success": True, "hostname": hostname, "action": ACTION}
        )

    assert turnstile.verify_token("t", settings_ok, post=reply(f"www.{HOST}")).outcome == "valid"
    for near_miss in (HOST.upper(), f"{HOST}.", f"evil.{HOST}", f"{HOST}:443", ""):
        assert turnstile.verify_token("t", settings_ok, post=reply(near_miss)).outcome == "rejected", near_miss


# --- 7-10: infrastructure failures fail closed (503) ---------------------------


@pytest.mark.parametrize(
    "token",
    [
        "timeout-1",  # 8
        "connect-1",  # 9
        "boom-1",  # unexpected exception
        "http500-1",  # non-200 reply
        "html-1",  # 10: not JSON
        "notbool-1",  # 10: success is not a boolean
        "list-1",  # 10: JSON but not an object
        "provider-internal-error",  # Cloudflare-side failure
        "provider-invalid-input-secret",  # our secret rejected
        "provider-missing-input-secret",
        "provider-bad-request",
    ],
)
def test_verification_failure_returns_503_and_fails_closed(
    client, turnstile_on, siteverify, scorer_calls, schema_ready, token
):
    res = _post(client, token=token)
    assert res.status_code == 503
    assert res.json() == {"detail": "Submission verification is unavailable"}
    assert scorer_calls == []
    assert _counts(schema_ready) == (0, 0)


@pytest.mark.parametrize(
    "missing",
    [
        {"TURNSTILE_SECRET_KEY": None},
        {"TURNSTILE_SECRET_KEY": ""},
        {"TURNSTILE_ALLOWED_HOSTNAMES": " , "},
        {"TURNSTILE_EXPECTED_ACTION": ""},
    ],
)
def test_missing_configuration_fails_closed(client, turnstile_on, siteverify, scorer_calls, schema_ready, missing):
    turnstile_on(**missing)
    res = _post(client, token="good-cfg")
    assert res.status_code == 503
    assert siteverify.calls == []
    assert scorer_calls == []
    assert _counts(schema_ready) == (0, 0)


def test_production_without_secret_fails_closed_even_if_disabled(  # 7
    client, turnstile_on, siteverify, scorer_calls, schema_ready
):
    turnstile_on(
        APP_ENV="production", API_AUTH_TOKEN=API_TOKEN, TURNSTILE_ENABLED="false", TURNSTILE_SECRET_KEY=None
    )
    for token in (None, "good-prod"):
        res = _post(client, token=token, bearer=API_TOKEN)
        assert res.status_code == 503
    assert scorer_calls == []
    assert _counts(schema_ready) == (0, 0)


def test_production_ignores_disable_flag_and_still_verifies(client, turnstile_on, siteverify, schema_ready):
    turnstile_on(APP_ENV="production", API_AUTH_TOKEN=API_TOKEN, TURNSTILE_ENABLED="false")
    assert _post(client, bearer=API_TOKEN).status_code == 403
    assert _post(client, token="good-prod-ok", bearer=API_TOKEN).status_code == 201
    assert _counts(schema_ready) == (1, 0)


def test_disabled_outside_production_skips_verification(client, turnstile_on, siteverify):
    turnstile_on(TURNSTILE_ENABLED="false", TURNSTILE_SECRET_KEY=None)
    assert _post(client).status_code == 201
    assert siteverify.calls == []


# --- 11-13: the Gmail channel ---------------------------------------------------


def test_gmail_channel_works_without_token(client, turnstile_on, siteverify, scorer_calls, schema_ready):  # 11
    res = _post(client, channel="gmail", source="gmail", external_id="gmail-msg-1")
    assert res.status_code == 201
    assert siteverify.calls == []
    assert scorer_calls == ["gmail-msg-1"]


def test_gmail_channel_still_requires_bearer_authentication(client, turnstile_on, siteverify, schema_ready):  # 12
    turnstile_on(APP_ENV="production", API_AUTH_TOKEN=API_TOKEN)
    for bearer in (None, "guessed-token"):
        res = _post(client, channel="gmail", bearer=bearer, source="gmail")
        assert res.status_code == 401
    assert _counts(schema_ready) == (0, 0)
    assert _post(client, channel="gmail", bearer=API_TOKEN, source="gmail").status_code == 201


def test_json_source_cannot_claim_the_gmail_exemption(client, turnstile_on, siteverify, schema_ready):  # 13
    for source in ("gmail", "Gmail", "gmail-lead-intake"):
        assert _post(client, source=source).status_code == 403
    assert _counts(schema_ready) == (0, 0)


@pytest.mark.parametrize("channel", ["Gmail", "GMAIL", "gmail-website", "website", "gmail,website", ""])
def test_only_the_exact_gmail_channel_is_exempt(client, turnstile_on, siteverify, schema_ready, channel):
    assert _post(client, channel=channel).status_code == 403
    assert _counts(schema_ready) == (0, 0)


def test_unknown_channel_still_needs_a_valid_token(client, turnstile_on, siteverify):
    assert _post(client, channel="website", token="good-chan").status_code == 201


def _workflow_nodes(path: Path) -> dict:
    return {n["name"]: n for n in json.loads(path.read_text(encoding="utf-8"))["nodes"]}


@pytest.mark.parametrize("workflow", WEBSITE_WORKFLOWS, ids=lambda p: p.name)
def test_website_workflows_send_only_fixed_headers_to_fastapi(workflow):
    """The public webhook caller can never set X-Intake-Channel on the API call."""
    text = workflow.read_text(encoding="utf-8")
    assert "X-Intake-Channel" not in text and "x-intake-channel" not in text.lower()
    assert "json.headers" not in text, "no node may read the public caller's request headers"
    # Task 05B: the only dynamic header is X-Turnstile-Token, taken from the
    # Prepare node's token (a webhook BODY field), never from request headers.
    allowed = {"content-type": "application/json", "x-turnstile-token": "={{ $json.turnstileToken }}"}
    for name in ("Create Lead in FastAPI", "Generate AI Insights"):
        params = _workflow_nodes(workflow)[name]["parameters"]
        headers = (params.get("headerParameters") or {}).get("parameters", [])
        assert all("{{" not in h["name"] for h in headers)
        assert all(allowed.get(h["name"].lower()) == h["value"] for h in headers), headers
        assert not params.get("jsonHeaders")


@pytest.mark.parametrize("workflow", WEBSITE_WORKFLOWS, ids=lambda p: p.name)
def test_webhook_payload_cannot_inject_a_channel(workflow):
    import subprocess

    code = _workflow_nodes(workflow)["Prepare Lead Payload"]["parameters"]["jsCode"]
    body = {
        **LEAD,
        "source": "gmail",
        "X-Intake-Channel": "gmail",
        "intake_channel": "gmail",
        "headers": {"X-Intake-Channel": "gmail"},
    }
    script = """
    const [code, body] = [process.argv[1], JSON.parse(process.argv[2])];
    const $input = { first: () => ({ json: { body, headers: { "x-intake-channel": "gmail" } } }) };
    const $env = { LEAD_API_BASE_URL: "https://api.example.test" };
    console.log(JSON.stringify(new Function("$input", "$env", code)($input, $env)[0].json));
    """
    out = json.loads(
        subprocess.run(["node", "-e", script, code, json.dumps(body)], capture_output=True, text=True, check=True).stdout
    )
    assert set(out["lead"]) == {"name", "email", "company", "message", "source", "external_id"}
    # The body's source is forwarded as data only; the API never treats it as a channel.
    assert out["lead"]["source"] == "gmail"


# --- 17: existing behaviour with verified submissions -------------------------


def test_duplicate_detection_unchanged_for_verified_submissions(client, turnstile_on, siteverify, schema_ready):
    assert _post(client, token="good-d1").status_code == 201
    dup = _post(client, token="good-d2")
    assert dup.status_code == 409
    assert dup.json()["detail"] == "A lead with this source and external_id already exists"
    assert _counts(schema_ready) == (1, 0)


def test_validation_unchanged_for_verified_submissions(client, turnstile_on, siteverify, schema_ready):
    res = _post(client, token="good-v1", email="not-an-email")
    assert res.status_code == 422
    assert _counts(schema_ready) == (0, 0)


def test_verification_runs_before_body_validation(client, turnstile_on, siteverify):
    # An unverified caller learns nothing about the payload schema.
    assert _post(client, email="not-an-email").status_code == 403


def test_score_is_identical_with_and_without_verification(client, turnstile_on, siteverify):
    verified = _post(client, token="good-s1", external_id="s-1").json()
    turnstile_on(TURNSTILE_ENABLED="false")
    unverified_dev = _post(client, external_id="s-2").json()
    assert (verified["score"], verified["score_reason"]) == (unverified_dev["score"], unverified_dev["score_reason"])


# --- secrecy -------------------------------------------------------------------


def test_secret_and_token_never_appear_in_logs_or_responses(client, turnstile_on, siteverify, caplog):
    caplog.set_level("DEBUG")
    token = "invalid-token-value-should-stay-private"
    responses = [
        _post(client, token=token),
        _post(client, token="timeout-private-token"),
        _post(client, token="good-private-token"),
    ]
    assert [r.status_code for r in responses] == [403, 503, 201]
    blob = caplog.text + "".join(r.text for r in responses)
    for secret in (SECRET, token, "timeout-private-token", "good-private-token"):
        assert secret not in blob
    assert "invalid-input-response" in caplog.text  # only Cloudflare's error code is logged
