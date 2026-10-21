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
    assert filters.get("readStatus") == "all"
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
    assert "import:workflow --input=/workflows/gmail-lead-intake.json" in entrypoint_content
    assert "publish:workflow --id=gmail-lead-intake" not in entrypoint_content, (
        "gmail-lead-intake must not be published/activated before OAuth configuration"
    )
