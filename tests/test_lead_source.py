"""Demo vs. website source classification (Task 04).

The page at demo/lead-form.html serves both / and /demo and switches between
them with history.pushState, so the lead source is resolved from
window.location.pathname at submit time. These tests cover:

- the pure LEAD_SOURCE helper in the page (run in Node.js),
- the page's real payload literals evaluated against a fake DOM,
- n8n "Prepare Lead Payload" forwarding the source unchanged,
- API persistence, retrieval, CSV export, duplicate detection and scoring
  for the demo source (isolated TEST_DATABASE_URL via the conftest fixtures).
"""

import csv
import io
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.models import LeadCreate
from app.scoring import score_lead_heuristic

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO_ROOT / "demo" / "lead-form.html"
WORKFLOWS = [REPO_ROOT / "n8n" / "lead-intake.json", REPO_ROOT / "n8n" / "lead-intake-ai-demo.json"]

WEBSITE = "website-contact-form"
DEMO = "website-demo-contact-form"

LEAD = {
    "name": "Jamie Example",
    "email": "jamie@example.com",
    "company": "Example Systems",
    "message": "We are evaluating a demo and pricing for an enterprise lead-intake workflow.",
    "external_id": "src-0001",
}


def _page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


def _source_block() -> str:
    page = _page()
    return page[page.index("/* LEAD_SOURCE_START"):page.index("/* LEAD_SOURCE_END */")]


def _resolve(pathnames: list[str]) -> list[str]:
    script = """
    const [block, paths] = [process.argv[1], JSON.parse(process.argv[2])];
    const resolveLeadSource = new Function(block + "; return resolveLeadSource;")();
    console.log(JSON.stringify(paths.map(resolveLeadSource)));
    """
    res = subprocess.run(
        ["node", "-e", script, _source_block(), json.dumps(pathnames)], capture_output=True, text=True, check=True
    )
    return json.loads(res.stdout)


def _browser_pathname(url: str) -> str:
    """What window.location.pathname is for a URL: never the query or hash."""
    return urlsplit(url).path or "/"


# --- 1-5: path classification ------------------------------------------------


def test_homepage_resolves_to_website_source():
    assert _resolve(["/"]) == [WEBSITE]


def test_demo_path_resolves_to_demo_source():
    assert _resolve(["/demo"]) == [DEMO]


def test_demo_trailing_slash_resolves_to_demo_source():
    assert _resolve(["/demo/"]) == [DEMO]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://leadintel.example/?view=demo", WEBSITE),
        ("https://leadintel.example/#demo", WEBSITE),
        ("https://leadintel.example/?utm_source=demo&source=website-demo", WEBSITE),
        ("https://leadintel.example/demo?utm_campaign=launch", DEMO),
        ("https://leadintel.example/demo/?view=landing#hero", DEMO),
    ],
)
def test_query_and_hash_do_not_change_classification(url, expected):
    assert _resolve([_browser_pathname(url)]) == [expected]


@pytest.mark.parametrize(
    "path",
    ["/demo//", "/demos", "/demo-old", "/demo/extra", "/demo.html", "/lead-form.html", "/DEMO", "/x/demo", "", "/index.html"],
)
def test_unknown_paths_are_not_demo(path):
    assert _resolve([path]) == [WEBSITE]


def test_both_sources_keep_contact_form_weighting():
    # The heuristic scorer adds +8 for any source containing "contact".
    assert "contact" in WEBSITE and "contact" in DEMO
    assert WEBSITE != DEMO


# --- 6: the page's real payload literals ---------------------------------------


def _payload_literals() -> list[str]:
    """Object literals the page sends or previews, taken verbatim from the page."""
    page = _page()
    submit = re.search(r"const payload = (\{.*?\n  \});", page, re.S).group(1)
    preview = re.search(r"const preview = (\{.*?\n  \});", page, re.S).group(1)
    return [submit, preview]


def _evaluate_payloads(pathname: str) -> list[dict]:
    script = """
    const [block, literals, pathname, fields] = [process.argv[1], JSON.parse(process.argv[2]), process.argv[3], JSON.parse(process.argv[4])];
    const document = { getElementById: (id) => ({ value: fields[id] ?? "" }) };
    const window = { location: { pathname } };
    const CONFIG = { webhookUrl: "/webhook/lead-intake" };
    const extId = "ext-from-test";
    const out = literals.map((lit) => new Function("document", "window", "CONFIG", "extId",
      block + "; return (" + lit + ");")(document, window, CONFIG, extId));
    console.log(JSON.stringify(out));
    """
    fields = {k: LEAD[k] for k in ("name", "email", "company", "message")}
    res = subprocess.run(
        ["node", "-e", script, _source_block(), json.dumps(_payload_literals()), pathname, json.dumps(fields)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


def test_page_reads_source_only_through_the_helper():
    page = _page()
    assert "CONFIG.source" not in page
    assert page.count("source: resolveLeadSource(window.location.pathname)") == 2


@pytest.mark.parametrize(("pathname", "expected"), [("/", WEBSITE), ("/demo", DEMO), ("/demo/", DEMO), ("/pricing", WEBSITE)])
def test_generated_payload_carries_path_source(pathname, expected):
    submit, preview = _evaluate_payloads(pathname)
    assert submit["source"] == expected
    assert preview["source"] == expected
    # Existing payload fields are unchanged.
    assert set(submit) == {"name", "email", "company", "message", "source", "external_id", "demo_insights"}
    assert submit["demo_insights"] is True
    assert {k: submit[k] for k in ("name", "email", "company", "message")} == {
        k: LEAD[k] for k in ("name", "email", "company", "message")
    }


# --- 7: n8n forwards the source unchanged -------------------------------------


def _run_prepare(workflow: Path, body: dict) -> dict:
    code = next(n for n in json.loads(workflow.read_text())["nodes"] if n["name"] == "Prepare Lead Payload")
    script = """
    const [code, body] = [process.argv[1], JSON.parse(process.argv[2])];
    const $input = { first: () => ({ json: { body } }) };
    const $env = { LEAD_API_BASE_URL: "https://api.example.test" };
    console.log(JSON.stringify(new Function("$input", "$env", code)($input, $env)[0].json));
    """
    res = subprocess.run(
        ["node", "-e", script, code["parameters"]["jsCode"], json.dumps(body)], capture_output=True, text=True, check=True
    )
    return json.loads(res.stdout)


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
@pytest.mark.parametrize("source", [WEBSITE, DEMO])
def test_n8n_prepare_forwards_source_unchanged(workflow, source):
    out = _run_prepare(workflow, {**LEAD, "source": source, "demo_insights": True})
    assert out["lead"]["source"] == source
    assert "demo_insights" not in out["lead"]
    nodes = {n["name"]: n for n in json.loads(workflow.read_text())["nodes"]}
    assert nodes["Create Lead in FastAPI"]["parameters"]["jsonBody"] == "={{ JSON.stringify($json.lead) }}"


# --- 8-10: API persistence, retrieval, export, duplicates, scoring ------------


def test_demo_source_persists_and_is_retrievable(client):
    created = client.post("/leads", json={**LEAD, "source": DEMO})
    assert created.status_code == 201
    assert created.json()["source"] == DEMO

    lead_id = created.json()["lead_id"]
    assert client.get(f"/leads/{lead_id}").json()["source"] == DEMO

    listed = client.get("/leads", params={"source": DEMO}).json()
    assert [item["source"] for item in listed["items"]] == [DEMO]
    assert client.get("/leads", params={"source": WEBSITE}).json()["items"] == []


def test_demo_and_website_sources_are_exported_unchanged(client):
    assert client.post("/leads", json={**LEAD, "source": WEBSITE}).status_code == 201
    assert client.post("/leads", json={**LEAD, "source": DEMO}).status_code == 201

    rows = list(csv.DictReader(io.StringIO(client.get("/leads/export").text)))
    assert sorted(r["source"] for r in rows) == sorted([WEBSITE, DEMO])

    demo_rows = list(csv.DictReader(io.StringIO(client.get("/leads/export", params={"source": DEMO}).text)))
    assert [(r["source"], r["external_id"]) for r in demo_rows] == [(DEMO, LEAD["external_id"])]


@pytest.mark.parametrize("source", [WEBSITE, DEMO])
def test_repeat_submission_from_same_path_is_duplicate(client, source):
    assert client.post("/leads", json={**LEAD, "source": source}).status_code == 201
    repeat = client.post("/leads", json={**LEAD, "source": source})
    assert repeat.status_code == 409
    assert repeat.json()["detail"] == "A lead with this source and external_id already exists"


def test_same_external_id_from_different_paths_is_two_leads(client):
    # Documented consequence of UNIQUE (source, external_id): one external_id
    # used on / and on /demo yields two separate leads, not a 409.
    assert client.post("/leads", json={**LEAD, "source": WEBSITE}).status_code == 201
    assert client.post("/leads", json={**LEAD, "source": DEMO}).status_code == 201


def test_demo_source_scores_exactly_like_website_source(client):
    website = client.post("/leads", json={**LEAD, "source": WEBSITE, "external_id": "s-1"}).json()
    demo = client.post("/leads", json={**LEAD, "source": DEMO, "external_id": "s-2"}).json()
    assert (demo["score"], demo["score_reason"]) == (website["score"], website["score_reason"])


@pytest.mark.parametrize(
    "message",
    [
        "We are evaluating a demo and pricing for an enterprise lead-intake workflow. We would like to discuss an integration this quarter.",
        "Exploring workflow automation solutions for our intake forms. Would love to review your security architecture and audit capabilities before starting.",
        "Hello, I would like to learn more.",
    ],
)
def test_heuristic_scores_identical_for_demo_presets(message):
    def score(source: str):
        return score_lead_heuristic(
            LeadCreate(name="X", email="x@example.com", company="Example", message=message, source=source, external_id="p")
        )

    assert score(DEMO) == score(WEBSITE)
