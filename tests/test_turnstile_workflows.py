"""Turnstile across n8n, Caddy, config generation and production settings (Task 05B).

Everything here is offline: workflow Code nodes and n8n expressions from the
checked-in JSON run in Node.js, shell config generators run against temporary
directories, and nothing calls n8n, Cloudflare or any production service.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
INTAKE = REPO_ROOT / "n8n" / "lead-intake.json"
GMAIL = REPO_ROOT / "n8n" / "gmail-lead-intake.json"
AI_DEMO = REPO_ROOT / "n8n" / "lead-intake-ai-demo.json"

LEAD_FIELDS = {"name", "email", "company", "message", "source", "external_id"}
BODY = {
    "name": "Jamie Example",
    "email": "jamie@example.com",
    "company": "Example Systems",
    "message": "Pricing for an enterprise rollout, please.",
    "source": "website-demo-contact-form",
    "external_id": "wf-0001",
    "demo_insights": True,
}
SUCCESS_ONLY = {
    "Extract Lead + Score", "Generate AI Insights", "Demo Insights Requested?", "Attach AI Insights",
    "Send Qualified to Telegram", "Send Manual Review to Telegram", "Send Qualified to WhatsApp",
    "Send Manual Review to WhatsApp", "Respond Created", "Respond Created with AI Insights",
}


def _wf(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _nodes(path: Path) -> dict:
    return {n["name"]: n for n in _wf(path)["nodes"]}


def _node_js(script: str, *args: str) -> object:
    res = subprocess.run(["node", "-e", script, *args], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def _prepare(path: Path, body: dict, headers: dict | None = None) -> dict:
    code = _nodes(path)["Prepare Lead Payload"]["parameters"]["jsCode"]
    script = """
    const [code, body, headers] = [process.argv[1], JSON.parse(process.argv[2]), JSON.parse(process.argv[3])];
    const $input = { first: () => ({ json: { body, headers } }) };
    const $env = { LEAD_API_BASE_URL: "https://api.example.test" };
    console.log(JSON.stringify(new Function("$input", "$env", code)($input, $env)[0].json));
    """
    return _node_js(script, code, json.dumps(body), json.dumps(headers or {}))


def _expr(expression: str, json_ctx: dict) -> object:
    """Evaluate an n8n `={{ ... }}` expression against $json."""
    assert expression.startswith("={{") and expression.endswith("}}")
    script = """
    const [src, ctx] = [process.argv[1], JSON.parse(process.argv[2])];
    console.log(JSON.stringify(new Function("$json", "return (" + src + ");")(ctx)));
    """
    return _node_js(script, expression[3:-2].strip(), json.dumps(json_ctx))


# --- 1-3: website token extraction and transport ------------------------------


def test_prepare_extracts_token_beside_the_lead():
    out = _prepare(INTAKE, {**BODY, "turnstile_token": "tok-123"})
    assert out["turnstileToken"] == "tok-123"
    assert set(out["lead"]) == LEAD_FIELDS  # whitelist unchanged; token not a lead field
    assert out["lead"]["source"] == BODY["source"]  # Task 04 classification passes through


@pytest.mark.parametrize("bad", [None, "", 123, ["tok"], {"t": 1}, "x" * 2049])
def test_prepare_drops_missing_or_malformed_tokens(bad):
    body = dict(BODY)
    if bad is not None:
        body["turnstile_token"] = bad
    out = _prepare(INTAKE, body)
    assert out["turnstileToken"] == ""  # FastAPI then answers 403 (fail closed)
    assert set(out["lead"]) == LEAD_FIELDS


def test_token_reaches_fastapi_only_as_x_turnstile_token_header():
    prepared = _prepare(INTAKE, {**BODY, "turnstile_token": "tok-123"})
    params = _nodes(INTAKE)["Create Lead in FastAPI"]["parameters"]
    headers = {h["name"]: h["value"] for h in params["headerParameters"]["parameters"]}
    assert set(headers) == {"Content-Type", "X-Turnstile-Token"}
    assert headers["Content-Type"] == "application/json"
    assert _expr(headers["X-Turnstile-Token"], prepared) == "tok-123"
    sent_body = json.loads(_expr(params["jsonBody"], prepared))
    assert set(sent_body) == LEAD_FIELDS and "tok-123" not in json.dumps(sent_body)
    node = _nodes(INTAKE)["Create Lead in FastAPI"]
    assert node["credentials"] == {"httpHeaderAuth": {"id": "fastapi-bearer-auth", "name": "FastAPI Bearer Auth"}}
    assert params["options"]["timeout"] == 10000
    assert params["options"]["response"]["response"] == {"fullResponse": True, "neverError": True}


def test_token_is_not_copied_into_any_other_node():
    for name, node in _nodes(INTAKE).items():
        text = json.dumps(node)
        if name in ("Prepare Lead Payload", "Create Lead in FastAPI"):
            continue
        assert "turnstile" not in text.lower(), name


# --- 4-5: Gmail channel ------------------------------------------------------------


def test_gmail_sends_fixed_trusted_channel_header_and_no_token():
    params = _nodes(GMAIL)["Create Lead in FastAPI"]["parameters"]
    assert params["headerParameters"]["parameters"] == [
        {"name": "Content-Type", "value": "application/json"},
        {"name": "X-Intake-Channel", "value": "gmail"},
    ]
    assert params["jsonBody"] == "={{ JSON.stringify($json.lead) }}"
    assert "turnstile" not in GMAIL.read_text(encoding="utf-8").lower()


# Last commit that changed n8n/gmail-lead-intake.json before the Turnstile
# release (367940a); its content is byte-identical at 9109f81 and e141b0c.
# Pinned rather than HEAD: once the release was committed, HEAD already
# contained the header and the comparison added it twice.
GMAIL_PRE_TURNSTILE_COMMIT = "c13d78f"


def test_gmail_workflow_otherwise_unchanged_from_pre_turnstile_baseline():
    base = json.loads(
        subprocess.check_output(["git", "show", f"{GMAIL_PRE_TURNSTILE_COMMIT}:n8n/gmail-lead-intake.json"], cwd=REPO_ROOT)
    )
    new = _wf(GMAIL)
    base_hdrs = {n["name"]: n for n in base["nodes"]}["Create Lead in FastAPI"]["parameters"]["headerParameters"]
    # Guard: the baseline must predate the trusted-channel header.
    assert all(h["name"] != "X-Intake-Channel" for h in base_hdrs["parameters"])
    base_hdrs["parameters"].append({"name": "X-Intake-Channel", "value": "gmail"})
    assert new == base


@pytest.mark.parametrize("path", [INTAKE, AI_DEMO], ids=lambda p: p.name)
def test_website_workflows_cannot_produce_the_gmail_exemption(path):
    text = path.read_text(encoding="utf-8").lower()
    assert "x-intake-channel" not in text and "intake_channel" not in text
    out = _prepare(path, {**BODY, "source": "gmail", "X-Intake-Channel": "gmail"}, {"x-intake-channel": "gmail"})
    assert set(out["lead"]) == LEAD_FIELDS


# --- 6-11: status routing -------------------------------------------------------


def _route_targets(path: Path, status) -> list[str]:
    wf = _wf(path)
    node = {n["name"]: n for n in wf["nodes"]}["Route on API Status"]["parameters"]
    rules = node["rules"]["values"]
    index = next(
        (i for i, r in enumerate(rules) if r["conditions"]["conditions"][0]["rightValue"] == status), len(rules)
    )
    assert node["options"]["fallbackOutput"] == "extra"
    return [e["node"] for e in wf["connections"]["Route on API Status"]["main"][index]]


def _reachable(path: Path, start: list[str]) -> set[str]:
    conns = _wf(path)["connections"]
    seen, queue = set(), list(start)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        for group in conns.get(name, {}).get("main", []):
            queue.extend(e["node"] for e in group)
    return seen


@pytest.mark.parametrize(
    ("status", "targets"),
    [
        (201, ["Demo Insights Requested?", "Generate AI Insights"]),
        (409, ["Respond Duplicate"]),
        (422, ["Respond Validation Failed"]),
        (403, ["Respond Verification Failed"]),
        (503, ["Respond Verification Unavailable"]),
        (401, ["Respond Upstream Error"]),
        (500, ["Respond Upstream Error"]),
        (None, ["Respond Upstream Error"]),  # network error: no statusCode
    ],
)
def test_route_on_api_status(status, targets):
    assert _route_targets(INTAKE, status) == targets


@pytest.mark.parametrize("status", [403, 503])
def test_rejections_never_reach_success_only_nodes(status):
    reached = _reachable(INTAKE, _route_targets(INTAKE, status))
    assert len(reached) == 1 and not (reached & SUCCESS_ONLY)


@pytest.mark.parametrize(
    ("name", "code", "outcome", "message"),
    [
        ("Respond Verification Failed", 403, "verification_failed",
         "Verification failed. Complete the verification and try again."),
        ("Respond Verification Unavailable", 503, "verification_unavailable",
         "Verification service is temporarily unavailable. Please try again shortly."),
    ],
)
def test_verification_responses_return_status_without_leaking_api_details(name, code, outcome, message):
    node = _nodes(INTAKE)[name]
    assert node["type"] == "n8n-nodes-base.respondToWebhook"
    assert node["parameters"]["options"] == {"responseCode": code}
    api_reply = {"statusCode": code, "body": {"detail": "internal: siteverify said timeout-or-duplicate"}}
    body = json.loads(_expr(node["parameters"]["responseBody"], api_reply))
    assert body == {"outcome": outcome, "apiStatus": code, "detail": message, "message": message}


def test_ai_demo_workflow_cannot_bypass_verification():
    wf = _wf(AI_DEMO)
    assert wf.get("active") is False  # unsupported; must stay inactive
    params = _nodes(AI_DEMO)["Create Lead in FastAPI"]["parameters"]
    assert params["headerParameters"]["parameters"] == [{"name": "Content-Type", "value": "application/json"}]
    # It never forwards a token, so with enforcement every call is answered 403
    # by FastAPI, which this workflow routes to its upstream-error response.
    assert "turnstile" not in AI_DEMO.read_text(encoding="utf-8").lower()
    reached = _reachable(AI_DEMO, _route_targets(AI_DEMO, 403))
    assert reached == {"Respond Upstream Error"}


# --- Caddy routing --------------------------------------------------------------


def test_caddy_proxies_only_the_lead_intake_webhook():
    text = (REPO_ROOT / "deploy" / "caddy" / "Caddyfile.render").read_text(encoding="utf-8")
    handles = re.findall(r"^\thandle( [^ {]+)? \{", text, re.M)
    assert handles == [" /webhook/lead-intake", " /webhook/*", ""]
    exact = text[text.index("handle /webhook/lead-intake {"):text.index("handle /webhook/* {")]
    assert "reverse_proxy {$N8N_WEBHOOK_TARGET:127.0.0.1:5678}" in exact
    wildcard = text[text.index("handle /webhook/* {"):]
    wildcard = wildcard[:wildcard.index("}")]
    assert "respond 404" in wildcard and "reverse_proxy" not in wildcard
    assert text.count("reverse_proxy") == 1


# --- frontend config generation ---------------------------------------------------


def _write_config(tmp_path: Path, **env: str) -> tuple[str, str]:
    script = REPO_ROOT / "deploy" / "docker" / "write-frontend-config.sh"
    res = subprocess.run(
        ["sh", str(script)], env={"LEAD_WWW_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, check=True,
    )
    return (tmp_path / "config.js").read_text(encoding="utf-8"), res.stderr


def test_container_config_contains_public_site_key_only(tmp_path):
    config, _ = _write_config(
        tmp_path, TURNSTILE_SITE_KEY="0x4AAAA-public_key", TURNSTILE_SECRET_KEY="0x4AAAA-SECRET-must-not-leak"
    )
    assert 'webhookUrl: "/webhook/lead-intake",' in config
    assert 'turnstileSiteKey: "0x4AAAA-public_key",' in config
    assert "SECRET" not in config and "turnstileDisabled" not in config


@pytest.mark.parametrize("key", ['abc";alert(1);//', "a b", "x" * 129, "</script>"])
def test_container_config_rejects_malformed_site_keys(tmp_path, key):
    config, err = _write_config(tmp_path, TURNSTILE_SITE_KEY=key)
    assert 'turnstileSiteKey: "",' in config and "refuse to submit" in err


def test_container_config_without_key_fails_closed(tmp_path):
    config, err = _write_config(tmp_path)
    assert 'turnstileSiteKey: "",' in config and "not set" in err


@pytest.mark.parametrize(("key", "expected"), [("0x4AAAA-public_key", "0x4AAAA-public_key"), ('bad"key', "")])
def test_pages_build_writes_validated_site_key(tmp_path, key, expected):
    target = tmp_path / "pages"
    subprocess.run(
        ["bash", str(REPO_ROOT / "deploy" / "bin" / "build-pages.sh")],
        env={"PAGES_BUILD_DIR": str(target), "TURNSTILE_SITE_KEY": key},
        capture_output=True, text=True, check=True,
    )
    config = (target / "config.js").read_text(encoding="utf-8")
    assert f'turnstileSiteKey: "{expected}",' in config and 'webhookUrl: "/webhook/lead-intake",' in config


# --- production enforcement ---------------------------------------------------------


def _render_api_env() -> dict:
    data = yaml.safe_load((REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))
    api = next(s for s in data["services"] if s["name"] == "leadintel-api")
    return {v["key"]: v for v in api["envVars"]}


def test_render_api_declares_turnstile_settings_without_secrets():
    env = _render_api_env()
    assert env["APP_ENV"]["value"] == "production"
    assert env["TURNSTILE_SECRET_KEY"] == {"key": "TURNSTILE_SECRET_KEY", "sync": False}
    assert env["TURNSTILE_ALLOWED_HOSTNAMES"]["value"] == "leadintel.sahelyon.com"
    assert env["TURNSTILE_EXPECTED_ACTION"]["value"] == "lead-intake"
    assert "TURNSTILE_ENABLED" not in env
    data = yaml.safe_load((REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))
    caddy = {v["key"]: v for v in next(s for s in data["services"] if s["name"] == "leadintel-caddy")["envVars"]}
    assert caddy["TURNSTILE_SITE_KEY"] == {"key": "TURNSTILE_SITE_KEY", "sync": False}


@pytest.mark.parametrize(
    "markers",
    [
        {"APP_ENV": "production"},  # render.yaml
        {"APP_ENV": "development", "RENDER": "true"},  # Render's own marker alone
        {"APP_ENV": "Production"},
        {"APP_ENV": "prod"},
    ],
)
def test_deployed_api_cannot_disable_turnstile(monkeypatch, markers):
    for key in ("APP_ENV", "RENDER"):
        monkeypatch.delenv(key, raising=False)
    render_env = {k: str(v["value"]) for k, v in _render_api_env().items() if "value" in v}
    for key, value in {**render_env, **markers, "TURNSTILE_ENABLED": "false"}.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None, database_url="postgresql:///unused")
    assert settings.is_production
    assert settings.turnstile_required is True
