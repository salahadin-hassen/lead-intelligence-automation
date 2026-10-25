"""AI insights in the normal lead-intake workflow and the /demo page.

Covers n8n/attach-ai-insights.js (the lead-id safeguard), its embedded copies
in both workflows, the rewired n8n/lead-intake.json graph, the unchanged
downstream scoring/qualification nodes, and the /demo AI insights card in
demo/lead-form.html. Everything runs locally in Node.js: no n8n instance, no
OpenAI request, no database.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INTAKE_PATH = REPO_ROOT / "n8n" / "lead-intake.json"
DEMO_PATH = REPO_ROOT / "n8n" / "lead-intake-ai-demo.json"
ATTACH_PATH = REPO_ROOT / "n8n" / "attach-ai-insights.js"
PAGE_PATH = REPO_ROOT / "demo" / "lead-form.html"

INSIGHTS = {
    "summary": "Asks for a quote for 20 sales reps.",
    "intent_category": "quote_request",
    "urgency": "medium",
    "recommended_next_action": "Send pricing for 20 seats.",
    "reason": "Explicit quote request with a timeframe.",
}
CREATED = {
    "statusCode": 201,
    "statusMessage": "Created",
    "headers": {"content-type": "application/json"},
    "body": {"lead_id": 23, "name": "Synthetic Prospect", "score": 75, "score_reason": "High-intent keywords (quote)"},
}


def _node(script: str, *args: str) -> object:
    res = subprocess.run(
        ["node", "-e", script, *args], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True
    )
    return json.loads(res.stdout)


def _attach(created: dict, insights_http: object) -> dict:
    script = """
    const { attachAiInsights } = require("./n8n/attach-ai-insights.js");
    console.log(JSON.stringify(attachAiInsights(JSON.parse(process.argv[1]), JSON.parse(process.argv[2]))));
    """
    return _node(script, json.dumps(created), json.dumps(insights_http))


def _workflow(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _nodes(path: Path) -> dict:
    return {n["name"]: n for n in _workflow(path)["nodes"]}


def _head_intake() -> dict:
    return json.loads(subprocess.check_output(["git", "show", "97656c7:n8n/lead-intake.json"], cwd=REPO_ROOT))


def _run_code_node(js_code: str, input_json: dict, named: dict, env: dict | None = None) -> dict:
    """Execute a runOnceForAllItems Code node body the way n8n calls it."""
    script = """
    const [code, input, named, env] = process.argv.slice(1).map((a, i) => (i === 0 ? a : JSON.parse(a)));
    const $input = { first: () => ({ json: input }), item: { json: input } };
    const $ = (name) => {
      if (!(name in named)) throw new Error("node not available: " + name);
      return { first: () => ({ json: named[name] }), item: { json: named[name] } };
    };
    const $env = new Proxy(env || {}, { get(t, k) { return t[k]; } });
    const $now = { toISO: () => "2026-10-25T00:00:00.000Z" };
    const $execution = { id: "test", mode: "manual" };
    const out = new Function("$input", "$", "$env", "$now", "$execution", code)($input, $, $env, $now, $execution);
    console.log(JSON.stringify(out));
    """
    return _node(script, js_code, json.dumps(input_json), json.dumps(named), json.dumps(env or {}))[0]["json"]


def _eval(expression: str, json_input: dict, named: dict) -> object:
    script = """
    const [expr, input, named] = process.argv.slice(1).map((a, i) => i === 0 ? a : JSON.parse(a));
    const body = expr.replace(/^=\\{\\{/, "").replace(/\\}\\}$/, "");
    const $ = (name) => ({ first: () => ({ json: named[name] }), item: { json: named[name] } });
    console.log(JSON.stringify(new Function("$json", "$", `return (${body});`)(input, $)));
    """
    return _node(script, expression, json.dumps(json_input), json.dumps(named))


# -----------------------------------------------------------------------------
# Lead-id safeguard and fail-open mapping (n8n/attach-ai-insights.js)
# -----------------------------------------------------------------------------


def test_completed_insights_for_the_created_lead_are_attached():
    out = _attach(CREATED, {"statusCode": 200, "body": {
        "lead_id": 23, "status": "completed", "insights": INSIGHTS, "model": "gpt-6-luna",
        "usage": {"input_tokens": 352}, "cached": False, "error": None,
    }})

    assert out["aiInsights"] == {
        "status": "completed", "lead_id": 23, "insights": INSIGHTS, "model": "gpt-6-luna", "error": None,
    }
    assert "usage" not in json.dumps(out["aiInsights"]), "cost metadata stays internal"


def test_create_response_shape_is_preserved_for_downstream_nodes():
    out = _attach(CREATED, {"statusCode": 200, "body": {"lead_id": 23, "status": "disabled"}})
    assert {k: v for k, v in out.items() if k != "aiInsights"} == CREATED


def test_regression_insights_of_another_lead_are_never_returned():
    """The earlier bug: the workflow created lead 23 but returned lead 21's insights."""
    other = dict(INSIGHTS, summary="Lead 21 private request")
    out = _attach(CREATED, {"statusCode": 200, "body": {"lead_id": 21, "status": "completed", "insights": other}})

    assert out["aiInsights"] == {"status": "unavailable", "lead_id": 23, "insights": None, "error": "lead_id_mismatch"}
    assert "Lead 21" not in json.dumps(out)


@pytest.mark.parametrize("bad_id", [None, "23", 23.5, 0])
def test_lead_id_must_match_exactly(bad_id):
    out = _attach(CREATED, {"statusCode": 200, "body": {"lead_id": bad_id, "status": "completed", "insights": INSIGHTS}})
    assert out["aiInsights"]["status"] == "unavailable"
    assert out["aiInsights"]["insights"] is None


@pytest.mark.parametrize("status,error", [
    ("disabled", None),
    ("disabled", "not_configured"),
    ("pending", None),
    ("failed", "timeout"),
    ("failed", "rate_limited"),
    ("failed", "insufficient_quota"),
    ("failed", "invalid_output"),
    ("failed", "auth_error"),
])
def test_non_completed_statuses_pass_through_without_insights(status, error):
    out = _attach(CREATED, {"statusCode": 200, "body": {"lead_id": 23, "status": status, "error": error, "insights": None}})
    assert out["aiInsights"] == {"status": status, "lead_id": 23, "insights": None, "error": error}


@pytest.mark.parametrize("insights_http,error", [
    ({"error": {"message": "connect ECONNREFUSED"}}, "insights_request_failed"),
    ({}, "insights_request_failed"),
    ({"statusCode": 503, "body": {"detail": "x"}}, "insights_http_503"),
    ({"statusCode": 401, "body": {"detail": "Unauthorized"}}, "insights_http_401"),
    ({"statusCode": 404, "body": {"detail": "Lead not found"}}, "insights_http_404"),
    ({"statusCode": 200, "body": "not an object"}, "insights_http_200"),
    ({"statusCode": 200, "body": {"lead_id": 23, "status": "weird"}}, "unexpected_insights_status"),
    ({"statusCode": 200, "body": {"lead_id": 23, "status": "completed", "insights": {**INSIGHTS, "urgency": "urgent"}}}, "invalid_insights"),
    ({"statusCode": 200, "body": {"lead_id": 23, "status": "completed", "insights": {**INSIGHTS, "intent_category": "sales"}}}, "invalid_insights"),
    ({"statusCode": 200, "body": {"lead_id": 23, "status": "completed", "insights": {"summary": "only"}}}, "invalid_insights"),
    ({"statusCode": 200, "body": {"lead_id": 23, "status": "completed", "insights": None}}, "invalid_insights"),
])
def test_http_failures_and_unexpected_shapes_fail_open(insights_http, error):
    out = _attach(CREATED, insights_http)
    assert out["aiInsights"] == {"status": "unavailable", "lead_id": 23, "insights": None, "error": error}
    assert out["statusCode"] == 201 and out["body"] == CREATED["body"], "lead creation result untouched"


def test_missing_created_lead_id_never_attaches_anything():
    out = _attach({"statusCode": 201, "body": {}}, {"statusCode": 200, "body": {"lead_id": 23, "status": "completed", "insights": INSIGHTS}})
    assert out["aiInsights"]["error"] == "missing_created_lead_id"
    assert out["aiInsights"]["insights"] is None


# -----------------------------------------------------------------------------
# Embedded copies and expressions
# -----------------------------------------------------------------------------


def _canonical_attach_source() -> str:
    src = ATTACH_PATH.read_text(encoding="utf-8")
    return src[src.index("function attachAiInsights("):src.index("if (typeof module")].strip()


@pytest.mark.parametrize("path", [INTAKE_PATH, DEMO_PATH])
def test_workflows_embed_the_canonical_attach_function(path):
    code = _nodes(path)["Attach AI Insights"]["parameters"]["jsCode"]
    assert _canonical_attach_source() in code
    assert "$('Create Lead in FastAPI').first().json" in code


@pytest.mark.parametrize("path", [INTAKE_PATH, DEMO_PATH])
def test_embedded_attach_node_runs_like_n8n(path):
    code = _nodes(path)["Attach AI Insights"]["parameters"]["jsCode"]
    stale_other_lead = {"statusCode": 200, "body": {"lead_id": 21, "status": "completed", "insights": INSIGHTS}}

    out = _run_code_node(code, stale_other_lead, {"Create Lead in FastAPI": CREATED})

    assert out["aiInsights"]["error"] == "lead_id_mismatch"
    good = {"statusCode": 200, "body": {"lead_id": 23, "status": "completed", "insights": INSIGHTS, "model": "gpt-6-luna"}}
    assert _run_code_node(code, good, {"Create Lead in FastAPI": CREATED})["aiInsights"]["insights"] == INSIGHTS


@pytest.mark.parametrize("path", [INTAKE_PATH, DEMO_PATH])
def test_insights_url_uses_this_executions_created_lead(path):
    url_expr = _nodes(path)["Generate AI Insights"]["parameters"]["url"]
    named = {"Prepare Lead Payload": {"apiLeadsUrl": "https://api.example.test/leads"}, "Create Lead in FastAPI": CREATED}
    stale = {"statusCode": 201, "body": {"lead_id": 21}}

    assert _eval(url_expr, stale, named) == "https://api.example.test/leads/23/insights"


@pytest.mark.parametrize("body,expected", [
    ({"demo_insights": True}, "true"),
    ({"demo_insights": "true"}, "false"),
    ({"demo_insights": 1}, "false"),
    ({}, "false"),
    (None, "false"),
])
def test_demo_flag_is_only_an_explicit_boolean_true(body, expected):
    nodes = _nodes(INTAKE_PATH)
    for name in ("Demo Insights Requested?", "Return AI Insights?"):
        cond = nodes[name]["parameters"]["rules"]["values"][0]["conditions"]["conditions"][0]
        assert cond["rightValue"] == "true" and cond["operator"] == {"type": "string", "operation": "equals"}
        assert _eval(cond["leftValue"], {}, {"Lead Webhook": {"body": body}}) == expected


def test_normal_response_body_is_unchanged_and_demo_body_adds_only_ai_insights():
    nodes = _nodes(INTAKE_PATH)
    head = {n["name"]: n for n in _head_intake()["nodes"]}
    assert nodes["Respond Created"]["parameters"] == head["Respond Created"]["parameters"]

    attached = {**CREATED, "aiInsights": {"status": "completed", "lead_id": 23, "insights": INSIGHTS, "model": "m", "error": None}}
    normal = json.loads(_eval(nodes["Respond Created"]["parameters"]["responseBody"], attached, {}))
    demo = json.loads(_eval(nodes["Respond Created with AI Insights"]["parameters"]["responseBody"], attached, {}))

    assert "ai_insights" not in normal
    assert {k: v for k, v in demo.items() if k != "ai_insights"} == normal
    assert demo["lead"]["lead_id"] == demo["ai_insights"]["lead_id"] == 23
    assert nodes["Respond Created with AI Insights"]["parameters"]["options"] == {"responseCode": 201}


# -----------------------------------------------------------------------------
# Graph: routing, one request per lead, unchanged notifications
# -----------------------------------------------------------------------------

DEMO_SWITCHES = {"Demo Insights Requested?", "Return AI Insights?"}


def _walk(wf: dict, start: str, output: int, demo: bool) -> list[str]:
    """Nodes reached from one output, resolving the two demo switches by flag."""
    conns = wf["connections"]
    reached, queue = [], [e["node"] for e in conns[start]["main"][output]]
    while queue:
        name = queue.pop(0)
        reached.append(name)
        outs = conns.get(name, {}).get("main", [])
        if name in DEMO_SWITCHES:
            outs = [outs[0]] if demo else outs[1:]
        elif name in ("Route on Qualification",):
            outs = outs  # all branches are possible; notifications are unchanged
        for group in outs:
            queue.extend(e["node"] for e in group if e["node"] not in reached and e["node"] not in queue)
    return reached


@pytest.mark.parametrize("demo,respond", [(False, "Respond Created"), (True, "Respond Created with AI Insights")])
def test_created_path_reaches_exactly_one_response_and_one_insights_call(demo, respond):
    wf = _workflow(INTAKE_PATH)
    reached = _walk(wf, "Route on API Status", 0, demo)
    responses = [n for n in reached if n.startswith("Respond ")]

    assert responses == [respond]
    assert reached.count("Generate AI Insights") == 1
    assert "Extract Lead + Score" in reached and "Send Qualified to Telegram" in reached


@pytest.mark.parametrize("output,respond", [(1, "Respond Duplicate"), (2, "Respond Validation Failed"), (3, "Respond Upstream Error")])
def test_duplicate_validation_and_upstream_paths_are_unchanged_and_never_call_ai(output, respond):
    wf = _workflow(INTAKE_PATH)
    head = _head_intake()
    assert wf["connections"]["Route on API Status"]["main"][output] == head["connections"]["Route on API Status"]["main"][output]
    assert _walk(wf, "Route on API Status", output, demo=True) == [respond]


def test_only_one_insights_request_node_without_retries():
    nodes = _nodes(INTAKE_PATH)
    generators = [n for n in nodes.values() if n["type"] == "n8n-nodes-base.httpRequest" and "/insights" in n["parameters"]["url"]]
    assert [n["name"] for n in generators] == ["Generate AI Insights"]
    node = generators[0]
    assert node["retryOnFail"] is False and node["onError"] == "continueRegularOutput"
    assert node["parameters"]["options"]["response"]["response"]["neverError"] is True
    assert node["parameters"]["options"]["timeout"] <= 30000


def test_existing_nodes_credentials_and_notification_wiring_are_unchanged():
    wf, head = _workflow(INTAKE_PATH), _head_intake()
    nodes = {n["name"]: n for n in wf["nodes"]}
    for old in head["nodes"]:
        new = nodes[old["name"]]
        changed = [k for k in set(old) | set(new) if old.get(k) != new.get(k)]
        assert changed in ([], ["position"]), f"{old['name']} changed: {changed}"
    rewired = {src for src in head["connections"] if head["connections"][src] != wf["connections"][src]}
    assert rewired == {"Route on API Status"}
    assert nodes["Generate AI Insights"]["credentials"] == nodes["Create Lead in FastAPI"]["credentials"]
    assert wf.get("active") == head.get("active")


def test_normal_response_is_sent_before_the_ai_call_and_ai_before_notifications():
    """executionOrder v1 runs sibling branches top-to-bottom by canvas position."""
    wf = _workflow(INTAKE_PATH)
    nodes = {n["name"]: n for n in wf["nodes"]}
    assert wf["settings"]["executionOrder"] == "v1"
    y = lambda name: nodes[name]["position"][1]  # noqa: E731
    assert y("Demo Insights Requested?") < y("Generate AI Insights")
    assert y("Return AI Insights?") < y("Extract Lead + Score")


@pytest.mark.parametrize("path", [INTAKE_PATH, DEMO_PATH])
def test_workflow_json_has_valid_connections_and_no_secrets(path):
    wf = _workflow(path)
    names = {n["name"] for n in wf["nodes"]}
    for src, targets in wf["connections"].items():
        assert src in names
        for group in targets["main"]:
            assert all(e["node"] in names for e in group)
    text = path.read_text(encoding="utf-8")
    for needle in ("sk-", "api.openai.com", "OPENAI_API_KEY", "Authorization"):
        assert needle not in text


@pytest.mark.parametrize("score,qualification", [(75, "qualified"), (48, "not_qualified"), (None, "manual_review")])
def test_deterministic_score_and_qualification_are_unchanged_after_attach(score, qualification):
    """Run the real, unchanged Extract + Qualification nodes on the attached item."""
    nodes = _nodes(INTAKE_PATH)
    created = {**CREATED, "body": {**CREATED["body"], "score": score}}
    for ai_body in ({"lead_id": 23, "status": "completed", "insights": INSIGHTS}, {"lead_id": 23, "status": "failed", "error": "timeout"}):
        attached = _attach(created, {"statusCode": 200, "body": ai_body})
        extracted = _run_code_node(nodes["Extract Lead + Score"]["parameters"]["jsCode"], attached, {})
        qualified = _run_code_node(
            nodes["Qualification"]["parameters"]["jsCode"], extracted,
            {"Prepare Lead Payload": {"qualifiedThreshold": 70, "thresholdSource": "workflow-default"}},
        )
        assert extracted["leadId"] == 23 and extracted["score"] == score
        assert qualified["qualification"] == qualification


def test_prepare_lead_payload_never_forwards_the_demo_flag_to_fastapi():
    code = _nodes(INTAKE_PATH)["Prepare Lead Payload"]["parameters"]["jsCode"]
    body = {"name": "n", "email": "n@example.com", "company": "c", "message": "m", "source": "s",
            "external_id": "e", "demo_insights": True}
    out = _run_code_node(code, {"body": body}, {}, env={"LEAD_API_BASE_URL": "https://api.example.test"})
    assert "demo_insights" not in out["lead"]
    assert set(out["lead"]) == {"name", "email", "company", "message", "source", "external_id"}


# -----------------------------------------------------------------------------
# /demo page: AI insights card
# -----------------------------------------------------------------------------


def _page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


def _ai_view_block() -> str:
    page = _page()
    return page[page.index("/* AI_INSIGHTS_VIEW_START"):page.index("/* AI_INSIGHTS_VIEW_END */")]


def _render(data: object) -> dict:
    """Run the page's describe+render code against fake elements that reject innerHTML."""
    script = """
    const [block, data] = [process.argv[1], JSON.parse(process.argv[2])];
    const ids = ["ai-insights-card", "ai-insights-badge", "ai-insights-note", "ai-insights-fields",
                 "ai-insights-summary", "ai-insights-intent", "ai-insights-urgency", "ai-insights-action", "ai-insights-reason"];
    const els = {};
    for (const id of ids) {
      els[id] = { textContent: "", className: "", hidden: id === "ai-insights-card" || id === "ai-insights-fields" };
      Object.defineProperty(els[id], "innerHTML", { set() { throw new Error("innerHTML used on " + id); } });
    }
    const document = { getElementById: (id) => els[id] || null };
    new Function("document", "data", block + "\\nrenderAiInsights(data === null ? null : describeAiInsights(data));")(document, data);
    const out = {};
    for (const id of ids) out[id] = { text: els[id].textContent, className: els[id].className, hidden: els[id].hidden };
    console.log(JSON.stringify(out));
    """
    return _node(script, _ai_view_block(), json.dumps(data))


def _created_response(ai_insights=None, lead_id=23):
    body = {"outcome": "created", "apiStatus": 201, "lead": {"lead_id": lead_id, "score": 75}, "score": 75}
    if ai_insights is not None:
        body["ai_insights"] = ai_insights
    return body


def test_demo_card_renders_the_returned_insights():
    out = _render(_created_response({"status": "completed", "lead_id": 23, "insights": INSIGHTS, "model": "gpt-6-luna"}))

    assert out["ai-insights-card"]["hidden"] is False
    assert out["ai-insights-fields"]["hidden"] is False
    assert out["ai-insights-badge"]["text"] == "Generated"
    assert "completed" in out["ai-insights-badge"]["className"]
    assert out["ai-insights-summary"]["text"] == INSIGHTS["summary"]
    assert out["ai-insights-intent"]["text"] == "Quote request"
    assert out["ai-insights-urgency"]["text"] == "Medium"
    assert out["ai-insights-action"]["text"] == INSIGHTS["recommended_next_action"]
    assert out["ai-insights-reason"]["text"] == INSIGHTS["reason"]
    assert "lead #23" in out["ai-insights-note"]["text"]


@pytest.mark.parametrize("ai,badge,phrase", [
    ({"status": "disabled", "lead_id": 23, "insights": None, "error": None}, "Disabled", "turned off"),
    ({"status": "failed", "lead_id": 23, "insights": None, "error": "insufficient_quota"}, "Failed", "credit balance"),
    ({"status": "failed", "lead_id": 23, "insights": None, "error": "timeout"}, "Failed", "timed out"),
    ({"status": "pending", "lead_id": 23, "insights": None, "error": None}, "Pending", "still being generated"),
    ({"status": "unavailable", "lead_id": 23, "insights": None, "error": "insights_http_503"}, "Unavailable", "could not be generated"),
    ({"status": "unavailable", "lead_id": 23, "insights": None, "error": "lead_id_mismatch"}, "Unavailable", "did not belong"),
    ({"status": "completed", "lead_id": 21, "insights": INSIGHTS}, "Unavailable", "did not belong"),
    ({"status": "completed", "lead_id": 23, "insights": {**INSIGHTS, "urgency": "urgent"}}, "Unavailable", "could not be generated"),
])
def test_demo_card_shows_unavailable_states_without_fabricated_content(ai, badge, phrase):
    out = _render(_created_response(ai))

    assert out["ai-insights-card"]["hidden"] is False
    assert out["ai-insights-fields"]["hidden"] is True
    assert out["ai-insights-badge"]["text"] == badge
    assert phrase in out["ai-insights-note"]["text"]
    assert "saved and scored" in out["ai-insights-note"]["text"]
    assert all(out[f"ai-insights-{k}"]["text"] == "" for k in ("summary", "intent", "urgency", "action", "reason"))


def test_demo_card_when_workflow_returns_no_insights():
    out = _render(_created_response())
    assert out["ai-insights-badge"]["text"] == "Not returned"
    assert out["ai-insights-fields"]["hidden"] is True


def test_demo_card_is_hidden_for_errors_and_duplicates():
    assert _render(None)["ai-insights-card"]["hidden"] is True


def test_ai_text_is_rendered_as_literal_text():
    hostile = "<img src=x onerror=alert(1)><script>alert(2)</script>"
    out = _render(_created_response({"status": "completed", "lead_id": 23, "insights": {**INSIGHTS, "summary": hostile, "reason": hostile}}))
    assert out["ai-insights-summary"]["text"] == hostile
    assert out["ai-insights-reason"]["text"] == hostile
    assert "innerHTML" not in _ai_view_block()


def test_demo_page_wiring_requests_insights_and_hides_card_on_other_outcomes():
    page = _page()
    submit = page[page.index("async function handleSubmit"):page.index('form.addEventListener("submit"')]
    assert "demo_insights: true," in submit
    assert 'id="ai-insights-card"' in page and "hidden>" in page[page.index('id="ai-insights-card"'):][:120]
    highlight = page[page.index("function highlightOutcome"):page.index("function setSubmitting")]
    created, rest = highlight.split('} else if (outcome === "duplicate") {', 1)
    assert "renderAiInsights(describeAiInsights(data));" in created
    assert rest.count("renderAiInsights(null);") == 2, "duplicate and error/network branches hide the card"
    reset = page[page.index('outcomeResetBtn.addEventListener("click"'):][:300]
    assert "renderAiInsights(null);" in reset
    assert "AI-generated suggestions" in page and "never changed by AI" in page


def test_double_submission_guard_is_still_in_place():
    page = _page()
    submit = page[page.index("async function handleSubmit"):]
    assert "if (isSubmitting) return;" in submit[:200]
    setter = page[page.index("function setSubmitting"):][:300]
    assert "submitBtn.disabled = submitting;" in setter


def test_ai_card_markup_uses_no_inline_event_handlers():
    card = _page()[_page().index('id="ai-insights-card"'):]
    card = card[:card.index("</p>\n            </div>") ]
    assert not re.search(r"\son[a-z]+=", card)
