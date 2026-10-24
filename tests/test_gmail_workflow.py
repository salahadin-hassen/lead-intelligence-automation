"""Automated unit and integration tests for Gmail lead ingestion workflow.

Tests cover:
- Normalization and deterministic field mapping (JavaScript harness).
- Automated notice and malformed message detection.
- Quoted reply stripping and length capping.
- FastAPI lead creation, qualification, heuristic scoring with source='gmail'.
- Deduplication and idempotency on replayed Gmail message IDs.
- Workflow JSON integrity, safe non-mutating mailbox parameters, and deployment entrypoint.
"""

import json
import subprocess
from pathlib import Path

import psycopg
import pytest

from app.models import ScoringResult
from app.routes.leads import get_scorer

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_node_normalization(payload: dict, config: dict | None = None) -> dict:
    """Helper to execute n8n/normalize-gmail.js directly through Node.js."""
    script = """
    const { normalizeGmailLead } = require("./n8n/normalize-gmail.js");
    const input = JSON.parse(process.argv[1]);
    const config = JSON.parse(process.argv[2]);
    const result = normalizeGmailLead(input, config);
    console.log(JSON.stringify(result));
    """
    res = subprocess.run(
        [
            "node",
            "-e",
            script,
            json.dumps(payload),
            json.dumps(config or {"apiBaseUrl": "http://127.0.0.1:8000"}),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


# -----------------------------------------------------------------------------
# 1. Normalization & Mapping Unit Tests
# -----------------------------------------------------------------------------


def test_normal_prospect_with_display_name():
    """Verify normal prospect email with display name and corporate domain."""
    raw_email = {
        "id": "18c9a1f2e3d4b5a6",
        "from": "Eleanor Vance <eleanor.vance@hillhouse-realty.com>",
        "subject": "Inquiry regarding LeadIntel Enterprise Tier",
        "text": "Hi team,\nWe have 35 sales agents and want to automate our lead qualification pipeline. Budget is $15k.",
    }
    result = _run_node_normalization(raw_email)

    assert result["status"] == "valid"
    lead = result["lead"]
    assert lead["name"] == "Eleanor Vance"
    assert lead["email"] == "eleanor.vance@hillhouse-realty.com"
    assert lead["company"] == "Hillhouse Realty"
    assert lead["source"] == "gmail"
    assert lead["external_id"] == "18c9a1f2e3d4b5a6"
    assert "Subject: Inquiry regarding LeadIntel Enterprise Tier" in lead["message"]
    assert "We have 35 sales agents" in lead["message"]


def test_message_with_no_display_name():
    """Verify email address with no display name derives clean name from localpart."""
    raw_email = {
        "id": "msg_nodisplay_002",
        "from": "marcus.brody@museum-archaeology.org",
        "subject": "Platform Evaluation",
        "text": "Please send over documentation and security whitepapers.",
    }
    result = _run_node_normalization(raw_email)

    assert result["status"] == "valid"
    lead = result["lead"]
    assert lead["name"] == "Marcus Brody"
    assert lead["email"] == "marcus.brody@museum-archaeology.org"
    assert lead["company"] == "Museum Archaeology"


def test_gmail_sender_address_no_company():
    """Verify free-mail address defaults company to 'Unspecified' without inventing names."""
    raw_email = {
        "id": "msg_freemail_003",
        "from": "clara.oswald@gmail.com",
        "subject": "Quick demo request",
        "text": "Looking for lead triage for a new consulting business.",
    }
    result = _run_node_normalization(raw_email)

    assert result["status"] == "valid"
    lead = result["lead"]
    assert lead["name"] == "Clara Oswald"
    assert lead["email"] == "clara.oswald@gmail.com"
    assert lead["company"] == "Unspecified"


def test_html_content_converted_to_clean_plain_text():
    """Verify HTML body is cleanly converted to readable plain text with preserved lines."""
    raw_email = {
        "id": "msg_html_004",
        "from": "Devon Woodcomb <devon@awesome-corp.com>",
        "subject": "HTML Inquiry",
        "html": (
            "<div><p>Hello LeadIntel team,</p>"
            "<p>We need an <b>automated</b> qualification pipeline.</p>"
            "<br><p>Budget: $50,000 &amp; timeline is Q4.</p></div>"
        ),
    }
    result = _run_node_normalization(raw_email)

    assert result["status"] == "valid"
    message = result["lead"]["message"]
    assert "<p>" not in message
    assert "<div>" not in message
    assert "<b>" not in message
    assert "automated qualification pipeline" in message
    assert "Budget: $50,000 & timeline is Q4." in message


def test_quoted_reply_chains_and_large_messages_stripped_and_truncated():
    """Verify quoted replies ('> ...', 'On ... wrote:') are stripped and large messages capped."""
    long_prospect_text = "We are interested in evaluating your API integrations.\n" * 150
    quoted_text = (
        long_prospect_text
        + "\n\nOn Tue, Oct 20, 2026 at 10:00 AM Sales Team wrote:\n"
        + "> Previous email thread content here...\n"
        + "> More quote history...\n"
    )

    raw_email = {
        "id": "msg_large_005",
        "from": "Sarah Walker <sarah@cia-consulting.gov>",
        "subject": "Re: Partnership Discovery",
        "text": quoted_text,
    }
    result = _run_node_normalization(raw_email)

    assert result["status"] == "valid"
    message = result["lead"]["message"]
    assert "Previous email thread content here" not in message
    assert "> More quote history" not in message
    assert len(message) <= 3200  # 3000 cap + subject prefix


@pytest.mark.parametrize(
    "raw_from,subject,headers",
    [
        (
            "mailer-daemon@googlemail.com",
            "Delivery Status Notification (Failure)",
            {},
        ),
        (
            "postmaster@domain.com",
            "Undelivered Mail Returned to Sender",
            {},
        ),
        (
            "john.doe@company.com",
            "Automatic reply: Out of Office until Monday",
            {"auto-submitted": "auto-replied"},
        ),
        (
            "noreply@service.com",
            "Notification alert",
            {},
        ),
    ],
)
def test_automatic_notices_flagged_as_skip(raw_from, subject, headers):
    """Verify bounces, auto-replies, and system notices are flagged as skip without calling API."""
    raw_email = {
        "id": "msg_auto_skip",
        "from": raw_from,
        "subject": subject,
        "headers": headers,
        "text": "Automated delivery failure body.",
    }
    result = _run_node_normalization(raw_email)

    assert result["status"] == "skip"
    assert result["reason"] == "automated_notice"


def _n8n_raw_trigger_item(**overrides) -> dict:
    """Item shaped like the Gmail Trigger output with Simplify off.

    n8n's parseRawEmail stores every header as its full raw line
    (headers[key] = header.line), and mailparser returns `from` as an
    address object - verified against the installed n8n 2.41.5.
    """
    item = {
        "id": "19a2b3c4d5e6f708",
        "threadId": "19a2b3c4d5e6f708",
        "labelIds": ["Label_42", "INBOX"],
        "headers": {
            "from": 'From: "Jane Doe" <jane.doe@acme-corp.com>',
            "subject": "Subject: Re: pricing question",
            "message-id": "Message-ID: <abc@mail.acme-corp.com>",
        },
        "from": {
            "value": [{"address": "jane.doe@acme-corp.com", "name": "Jane Doe"}],
            "text": '"Jane Doe" <jane.doe@acme-corp.com>',
        },
        "subject": "Re: pricing question",
        "text": "Hello & welcome",
        "html": "<p>Hello &amp; welcome</p>",
    }
    item.update(overrides)
    return item


def test_n8n_raw_trigger_item_maps_to_lead_contract():
    """Verify the real Gmail Trigger field shapes map onto the LeadCreate fields."""
    result = _run_node_normalization(_n8n_raw_trigger_item())

    assert result["status"] == "valid"
    assert result["lead"] == {
        "name": "Jane Doe",
        "email": "jane.doe@acme-corp.com",
        "company": "Acme Corp",
        "message": "Subject: Re: pricing question\n\nHello & welcome",
        "source": "gmail",
        "external_id": "19a2b3c4d5e6f708",
    }


@pytest.mark.parametrize("value", ["auto-replied", "auto-generated", "auto-notified"])
def test_n8n_raw_auto_submitted_header_line_flagged_as_skip(value):
    """Verify Auto-Submitted is honoured in n8n's full-line header format (RFC 3834)."""
    item = _n8n_raw_trigger_item()
    item["headers"]["auto-submitted"] = f"Auto-Submitted: {value}"

    result = _run_node_normalization(item)

    assert result["status"] == "skip"
    assert result["reason"] == "automated_notice"


def test_n8n_raw_auto_submitted_no_is_not_skipped():
    """Verify 'Auto-Submitted: no' marks a human-sent message (RFC 3834)."""
    item = _n8n_raw_trigger_item()
    item["headers"]["auto-submitted"] = "Auto-Submitted: no"

    assert _run_node_normalization(item)["status"] == "valid"


def test_long_subject_is_capped_but_still_used_for_automated_detection():
    """Verify the subject is capped like the body (LeadCreate has no length limits)."""
    long_subject = "Pricing for enterprise rollout " + "x" * 5000
    result = _run_node_normalization(_n8n_raw_trigger_item(subject=long_subject))

    first_line = result["lead"]["message"].split("\n", 1)[0]
    assert first_line == "Subject: " + long_subject[:200] + "… [truncated]"

    bounce = _run_node_normalization(
        _n8n_raw_trigger_item(subject="y" * 300 + " Undelivered Mail Returned to Sender")
    )
    assert bounce["reason"] == "automated_notice"


def test_folded_subject_whitespace_does_not_break_message_layout():
    result = _run_node_normalization(_n8n_raw_trigger_item(subject="Demo\r\n request"))
    assert result["lead"]["message"].startswith("Subject: Demo request\n\nHello")


@pytest.mark.parametrize(
    "raw_email,expected_detail",
    [
        (
            {"from": "alice@company.com", "subject": "No ID"},
            "Missing stable Gmail message ID",
        ),
        (
            {"id": "msg_no_from", "subject": "No From"},
            "Missing or unparseable sender email in From header",
        ),
        (
            {
                "id": "msg_bad_email",
                "from": "Invalid Sender <not-an-email>",
                "subject": "Bad email",
            },
            "Missing or unparseable sender email in From header",
        ),
    ],
)
def test_malformed_email_data_flagged_as_skip(raw_email, expected_detail):
    """Verify missing ID or invalid email is rejected at normalization step."""
    result = _run_node_normalization(raw_email)
    assert result["status"] == "skip"
    assert result["reason"] == "malformed_data"
    assert expected_detail in result["detail"]


# -----------------------------------------------------------------------------
# 2. FastAPI Pipeline & Scoring Integration Tests (Isolated Test Database)
# -----------------------------------------------------------------------------


def test_fastapi_creates_gmail_lead_with_scoring(client):
    """Verify FastAPI accepts source='gmail', applies qualification and heuristic scoring."""
    payload = {
        "name": "David Rossi",
        "email": "d.rossi@fbi-behavioral.gov",
        "company": "FBI Behavioral Unit",
        "message": (
            "Subject: Enterprise Security & Lead Intelligence Trial\n\n"
            "We are evaluating your autonomous lead triage platform. "
            "Please confirm SOC2 compliance, audit trails, and schedule an enterprise pilot."
        ),
        "source": "gmail",
        "external_id": "gmail_msg_001_live",
    }
    res = client.post("/leads", json=payload)
    assert res.status_code == 201
    body = res.json()

    assert body["name"] == payload["name"]
    assert body["email"] == payload["email"]
    assert body["source"] == "gmail"
    assert body["status"] == "new"
    assert isinstance(body["score"], int)
    assert body["score"] >= 70  # high intent keywords + corporate domain + length
    assert "direct email channel (gmail)" in body["score_reason"]


def test_replayed_gmail_message_id_returns_409(client, schema_ready: str):
    """Verify replaying the same Gmail message ID produces an HTTP 409 conflict idempotently."""
    payload = {
        "name": "Morgan Stanley",
        "email": "inquiry@morgan-advisors.com",
        "company": "Morgan Advisors",
        "message": "Subject: API Integration Inquiry\n\nLooking for lead qualification.",
        "source": "gmail",
        "external_id": "gmail_msg_dedup_001",
    }

    # 1. First ingestion succeeds (HTTP 201)
    res1 = client.post("/leads", json=payload)
    assert res1.status_code == 201

    # 2. Replay with identical (source, external_id) returns HTTP 409 Conflict
    res2 = client.post("/leads", json=payload)
    assert res2.status_code == 409
    assert "already exists" in res2.json()["detail"]

    # 3. Verify exactly 1 row exists in database
    with psycopg.connect(schema_ready, autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM leads WHERE source = 'gmail' AND external_id = 'gmail_msg_dedup_001';"
        ).fetchone()[0]
        assert count == 1, "Duplicate replay must not create a secondary database row"


def test_retry_after_ambiguous_write_keeps_scoring_and_cannot_identify_lead(client, schema_ready: str):
    """Model a create whose response timed out, followed by the n8n retry.

    The first request persisted and scored the lead; the retry gets 409. The
    409 body carries no lead id or score, which is why the workflow cannot
    resume qualification from it (docs/gmail-lead-ingestion.md, Section 5).
    """
    payload = {
        "name": "Jane Doe",
        "email": "jane.doe@acme-corp.com",
        "company": "Acme Corp",
        "message": "Subject: Pricing\n\nWe want a demo and pricing for 40 seats this quarter.",
        "source": "gmail",
        "external_id": "gmail_msg_timeout_retry",
    }
    assert client.post("/leads", json=payload).status_code == 201  # response "lost"

    retry = client.post("/leads", json=payload)
    assert retry.status_code == 409
    assert set(retry.json()) == {"detail"}, "API change: revisit the duplicate recovery design"

    with psycopg.connect(schema_ready, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT score, status FROM leads WHERE source = 'gmail' AND external_id = %s",
            (payload["external_id"],),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] is not None, "scoring ran during the first (timed-out) request"
    assert rows[0][1] == "new"


def test_different_sources_same_external_id_dont_conflict(client):
    """Verify composite uniqueness (source, external_id) permits identical external_id across different channels."""
    external_id = "shared-id-12345"
    payload_web = {
        "name": "Web User",
        "email": "web@example.com",
        "company": "Web Co",
        "message": "Enquiry from website.",
        "source": "website",
        "external_id": external_id,
    }
    payload_gmail = {
        "name": "Gmail User",
        "email": "gmail@example.com",
        "company": "Gmail Co",
        "message": "Enquiry from email.",
        "source": "gmail",
        "external_id": external_id,
    }

    res_web = client.post("/leads", json=payload_web)
    assert res_web.status_code == 201

    res_gmail = client.post("/leads", json=payload_gmail)
    assert res_gmail.status_code == 201, "Different sources must not collide on external_id"


# -----------------------------------------------------------------------------
# 3. Workflow Manifest & Deployment Integrity Tests
# -----------------------------------------------------------------------------


def test_gmail_workflow_manifest_integrity():
    """Verify n8n/gmail-lead-intake.json structure, inactive default status, and safe filter scope."""
    workflow_path = REPO_ROOT / "n8n" / "gmail-lead-intake.json"
    assert workflow_path.exists(), "gmail-lead-intake.json must exist in n8n directory"

    data = json.loads(workflow_path.read_text(encoding="utf-8"))

    # 1. Must be inactive by default pending operator OAuth configuration
    assert data.get("active") is False, "Gmail workflow MUST be imported inactive"
    assert data.get("id") == "gmail-lead-intake"

    # 2. Verify all 16 required nodes are present
    node_map = {n["name"]: n for n in data["nodes"]}
    expected_nodes = [
        "Gmail Trigger",
        "Normalize Email Lead",
        "Filter Valid Leads",
        "Log Skipped Message",
        "Create Lead in FastAPI",
        "Route on API Status",
        "Log Idempotent Duplicate",
        "Log Validation Failure",
        "Log Auth Error",
        "Log Upstream Error",
        "Extract Lead + Score",
        "Qualification",
        "Route on Qualification",
        "Log Priority Route",
        "Log Normal Route",
        "Log Manual Review",
    ]
    for name in expected_nodes:
        assert name in node_map, f"Missing required node: {name}"

    # 3. Verify Gmail Trigger configuration: dedicated label, exclude spam/trash, readStatus all
    trigger_node = node_map["Gmail Trigger"]
    params = trigger_node.get("parameters", {})
    filters = params.get("filters", {})
    assert filters.get("labelIds") == ["LeadIntel_Leads"]
    assert filters.get("readStatus") == "both"
    assert "-in:spam" in filters.get("q", "")
    assert "-in:trash" in filters.get("q", "")
    assert params.get("options", {}).get("downloadAttachments") is False
    assert params.get("options", {}).get("includeSpamTrash") is False

    # 4. Verify no mutating operations exist in workflow (no send, delete, mark read, archive)
    for node in data["nodes"]:
        node_type = node.get("type", "")
        if "gmail" in node_type.lower() and node_type != "n8n-nodes-base.gmailTrigger":
            operation = node.get("parameters", {}).get("operation", "")
            assert operation not in (
                "delete",
                "send",
                "reply",
                "addLabels",
                "removeLabels",
            ), "Gmail ingestion must remain read-only"

    # 5. Verify connections integrity: every source and destination exists
    connections = data.get("connections", {})
    for src_name, targets in connections.items():
        assert src_name in node_map, f"Connection references non-existent node {src_name}"
        for output_group in targets.get("main", []):
            for edge in output_group:
                target_node = edge.get("node")
                assert target_node in node_map, (
                    f"Connection references non-existent target {target_node}"
                )


def _load_gmail_workflow_nodes() -> dict:
    data = json.loads((REPO_ROOT / "n8n" / "gmail-lead-intake.json").read_text(encoding="utf-8"))
    return {n["name"]: n for n in data["nodes"]}


def _canonical_normalizer_source() -> str:
    source = (REPO_ROOT / "n8n" / "normalize-gmail.js").read_text(encoding="utf-8")
    start = source.index("function normalizeGmailLead(")
    end = source.index("// Support CommonJS and ES Module exports")
    return source[start:end].strip()


def test_embedded_normalizer_matches_canonical_module():
    """Verify the Code node runs the same normalizeGmailLead that the unit tests exercise."""
    js_code = _load_gmail_workflow_nodes()["Normalize Email Lead"]["parameters"]["jsCode"]
    assert _canonical_normalizer_source() in js_code, (
        "Normalize Email Lead must embed n8n/normalize-gmail.js verbatim; copy it back in"
    )


def test_embedded_normalizer_handles_every_polled_message_with_env_blocked():
    """Run the Code node as n8n does, once per item, with $env denied (n8n 2.x default)."""
    js_code = _load_gmail_workflow_nodes()["Normalize Email Lead"]["parameters"]["jsCode"]
    items = [
        _n8n_raw_trigger_item(id="msg_batch_1"),
        _n8n_raw_trigger_item(id="msg_batch_2"),
    ]
    script = """
    const [code, items] = [process.argv[1], JSON.parse(process.argv[2])];
    const $env = new Proxy({}, { get() { throw new Error("access to env vars denied"); } });
    const run = new Function("$input", "$env", code);
    console.log(JSON.stringify(items.map((json) => run({ item: { json } }, $env))));
    """
    res = subprocess.run(
        ["node", "-e", script, js_code, json.dumps(items)],
        capture_output=True,
        text=True,
        check=True,
    )
    outputs = json.loads(res.stdout)

    assert [o["json"]["lead"]["external_id"] for o in outputs] == ["msg_batch_1", "msg_batch_2"]
    for output in outputs:
        assert output["json"]["apiLeadsUrl"] == "https://leadintel-api-2y0i.onrender.com/leads"


def test_gmail_code_nodes_process_each_item():
    """Verify no Code node silently drops all but the first message of a poll."""
    for name, node in _load_gmail_workflow_nodes().items():
        if node["type"] != "n8n-nodes-base.code":
            continue
        params = node["parameters"]
        assert params.get("mode") == "runOnceForEachItem", f"{name} must run once per item"
        assert ".first()" not in params["jsCode"], f"{name} must read paired items, not .first()"


def test_gmail_trigger_read_status_is_a_valid_option():
    """Verify readStatus uses n8n's 'both' value; any other value is appended as is:<value>."""
    filters = _load_gmail_workflow_nodes()["Gmail Trigger"]["parameters"]["filters"]
    assert filters["readStatus"] == "both"


def test_gmail_lead_post_retries_transient_failures():
    """Verify timeouts / network errors are retried; replays are safe because of the 409 path."""
    node = _load_gmail_workflow_nodes()["Create Lead in FastAPI"]
    assert node.get("retryOnFail") is True
    assert node.get("maxTries", 0) >= 2
    options = node["parameters"]["options"]
    assert options["response"]["response"]["neverError"] is True


def test_duplicate_branch_flags_qualification_as_not_evaluated():
    """A 409 may follow a timed-out create; the record must not imply qualification ran."""
    js_code = _load_gmail_workflow_nodes()["Log Idempotent Duplicate"]["parameters"]["jsCode"]
    script = """
    const code = process.argv[1];
    const run = new Function("$input", "$", code);
    const normalized = { json: { lead: { external_id: "msg_retry_1" } } };
    const http = { json: { statusCode: 409, body: { detail: "exists" } } };
    console.log(JSON.stringify(run({ item: http }, () => ({ item: normalized }))));
    """
    res = subprocess.run(["node", "-e", script, js_code], capture_output=True, text=True, check=True)
    record = json.loads(res.stdout)["json"]

    assert record["external_id"] == "msg_retry_1"
    assert record["qualification"] == "not_evaluated"
    assert "earlier attempt" in record["reviewHint"]


def test_gmail_workflow_has_no_side_effects_after_lead_creation():
    """Everything after the create is logging only, so a skipped qualification
    branch loses a log record, not a notification or status update. Adding a
    side effect here requires the API change described in the docs first."""
    nodes = _load_gmail_workflow_nodes()
    http_nodes = [n for n, node in nodes.items() if node["type"] == "n8n-nodes-base.httpRequest"]
    assert http_nodes == ["Create Lead in FastAPI"]
    allowed = {
        "n8n-nodes-base.gmailTrigger",
        "n8n-nodes-base.code",
        "n8n-nodes-base.switch",
        "n8n-nodes-base.httpRequest",
    }
    assert {node["type"] for node in nodes.values()} <= allowed


def test_gmail_workflow_has_no_mailbox_mutating_nodes():
    """Verify the trigger is the only Gmail node and nothing calls the Gmail API directly."""
    for name, node in _load_gmail_workflow_nodes().items():
        node_type = node["type"].lower()
        if "gmail" in node_type:
            assert node["type"] == "n8n-nodes-base.gmailTrigger", f"{name} is a mutating-capable Gmail node"
        if node_type == "n8n-nodes-base.httprequest":
            assert "googleapis" not in json.dumps(node["parameters"]), f"{name} calls a Google API"


def test_docker_and_entrypoint_workflow_provisioning():
    """Verify Dockerfile.n8n and n8n-entrypoint.sh provision both workflows without forced overwrites."""
    dockerfile_path = REPO_ROOT / "deploy" / "docker" / "Dockerfile.n8n"
    assert dockerfile_path.exists()
    dockerfile_content = dockerfile_path.read_text(encoding="utf-8")
    assert "COPY n8n/lead-intake.json /workflows/lead-intake.json" in dockerfile_content
    assert "COPY n8n/gmail-lead-intake.json /workflows/gmail-lead-intake.json" in dockerfile_content

    entrypoint_path = REPO_ROOT / "deploy" / "docker" / "n8n-entrypoint.sh"
    assert entrypoint_path.exists()
    entrypoint_content = entrypoint_path.read_text(encoding="utf-8")
    # Must preserve existing workflows and avoid forced publishing on restart
    assert "export:workflow --id=gmail-lead-intake" in entrypoint_content
    assert "GMAIL_WF=/workflows/gmail-lead-intake.json" in entrypoint_content
    # Import behaviour is exercised end to end in tests/test_n8n_gmail_import.py
    assert 'n8n import:workflow --input="$GMAIL_TMP/import.json"' in entrypoint_content
    assert "publish:workflow --id=gmail-lead-intake" not in entrypoint_content, (
        "gmail-lead-intake must not be published/activated before OAuth configuration"
    )
