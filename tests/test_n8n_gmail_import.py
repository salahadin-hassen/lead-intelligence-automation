"""Functional tests for the Gmail-only workflow import in deploy/docker/n8n-entrypoint.sh.

The entrypoint runs against a fake `n8n` CLI that keeps workflows as JSON
files. The fake mirrors n8n 2.41.7 behaviour verified against the real CLI:
export of an unknown id fails with "No workflows found with specified
filters", import upserts by id and stores the workflow deactivated
(active=false, activeVersionId=null) with a fresh versionId, and publish sets
activeVersionId. No real n8n database is touched.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "deploy" / "docker" / "n8n-entrypoint.sh"
HELPER = REPO_ROOT / "deploy" / "docker" / "gmail-workflow-import.js"
GMAIL_ID = "gmail-lead-intake"
WEBSITE_ID = "lead-intake"

FAKE_N8N = """#!{python}
import json, os, sys, uuid
from pathlib import Path

db = Path(os.environ["FAKE_N8N_DB"])
with open(db / "calls.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
cmd = sys.argv[1]
flags = dict(a[2:].split("=", 1) for a in sys.argv[2:] if a.startswith("--") and "=" in a)

if cmd == "start":
    sys.exit(0)
if os.environ.get("FAKE_N8N_DB_DOWN") == "1":
    print("Error: connect ECONNREFUSED 127.0.0.1:5432", file=sys.stderr)
    sys.exit(1)
if cmd == "export:workflow":
    stored = db / (flags["id"] + ".json")
    if not stored.exists():
        print("Error: No workflows found with specified filters", file=sys.stderr)
        sys.exit(1)
    Path(flags["output"]).write_text(json.dumps([json.loads(stored.read_text())]))
elif cmd == "import:workflow":
    if os.environ.get("FAKE_N8N_IMPORT_FAIL") == "1":
        print("Error: import failed", file=sys.stderr)
        sys.exit(1)
    data = json.loads(Path(flags["input"]).read_text())
    for wf in data if isinstance(data, list) else [data]:
        wf.update(active=False, activeVersionId=None, versionId=str(uuid.uuid4()))
        (db / (wf["id"] + ".json")).write_text(json.dumps(wf))
        with open(db / "imported.log", "a") as log:
            log.write(wf["id"] + "\\n")
elif cmd == "publish:workflow":
    stored = db / (flags["id"] + ".json")
    wf = json.loads(stored.read_text())
    wf["activeVersionId"] = wf["versionId"]
    stored.write_text(json.dumps(wf))
"""


class FakeN8n:
    def __init__(self, tmp_path: Path):
        self.db = tmp_path / "db"
        self.workflows = tmp_path / "workflows"
        bin_dir = tmp_path / "bin"
        for d in (self.db, self.workflows, bin_dir):
            d.mkdir()
        shutil.copy(REPO_ROOT / "n8n" / "lead-intake.json", self.workflows)
        shutil.copy(REPO_ROOT / "n8n" / "gmail-lead-intake.json", self.workflows)
        shutil.copy(HELPER, self.workflows)
        fake = bin_dir / "n8n"
        fake.write_text(FAKE_N8N.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.entrypoint = tmp_path / "entrypoint.sh"
        self.entrypoint.write_text(
            ENTRYPOINT.read_text(encoding="utf-8").replace("/workflows/", f"{self.workflows}/")
        )
        node_dir = Path(shutil.which("node")).parent
        self.path = f"{bin_dir}:{node_dir}:/usr/bin:/bin"

    def boot(self, **env: str) -> str:
        (self.db / "calls.log").write_text("")
        (self.db / "imported.log").write_text("")
        res = subprocess.run(
            ["sh", str(self.entrypoint)],
            env={"PATH": self.path, "FAKE_N8N_DB": str(self.db), **env},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        return res.stdout + res.stderr

    def calls(self) -> list[str]:
        return (self.db / "calls.log").read_text().splitlines()

    def imports(self) -> list[str]:
        """Workflow ids written by import:workflow during the last boot."""
        return (self.db / "imported.log").read_text().splitlines()

    def stored(self, workflow_id: str) -> dict:
        return json.loads((self.db / f"{workflow_id}.json").read_text())

    def stored_raw(self, workflow_id: str) -> bytes:
        return (self.db / f"{workflow_id}.json").read_bytes()

    def bind_gmail_manually(self) -> None:
        """Simulate the operator binding credentials and the label in the n8n UI."""
        wf = self.stored(GMAIL_ID)
        for node in wf["nodes"]:
            creds = node.get("credentials", {})
            if "gmailOAuth2" in creds:
                creds["gmailOAuth2"] = {"id": "uiBoundGmail01", "name": "Gmail account"}
            if "httpHeaderAuth" in creds:
                creds["httpHeaderAuth"] = {"id": "uiBoundApi0002", "name": "FastAPI Bearer Auth"}
            if node["type"] == "n8n-nodes-base.gmailTrigger":
                node["parameters"]["filters"]["labelIds"] = ["Label_9001"]
        (self.db / f"{GMAIL_ID}.json").write_text(json.dumps(wf))

    def change_repo_gmail_workflow(self) -> None:
        path = self.workflows / "gmail-lead-intake.json"
        wf = json.loads(path.read_text())
        wf["name"] += " (next release)"
        path.write_text(json.dumps(wf, indent=2))


def _gmail_bindings(wf: dict) -> dict:
    nodes = {n["name"]: n for n in wf["nodes"]}
    return {
        "gmail": nodes["Gmail Trigger"]["credentials"]["gmailOAuth2"]["id"],
        "api": nodes["Create Lead in FastAPI"]["credentials"]["httpHeaderAuth"]["id"],
        "label": nodes["Gmail Trigger"]["parameters"]["filters"]["labelIds"],
    }


@pytest.fixture
def n8n(tmp_path: Path) -> FakeN8n:
    fake = FakeN8n(tmp_path)
    fake.boot()  # first boot: both workflows imported
    fake.bind_gmail_manually()
    return fake


def test_first_boot_imports_gmail_inactive_with_source_hash(tmp_path: Path):
    fake = FakeN8n(tmp_path)
    fake.boot()

    gmail = fake.stored(GMAIL_ID)
    assert gmail["active"] is False and gmail["activeVersionId"] is None
    assert len(gmail["meta"]["leadintelSourceSha256"]) == 64
    assert "publish:workflow --id=gmail-lead-intake" not in fake.calls()
    assert fake.stored(WEBSITE_ID)["activeVersionId"], "website workflow is still published"


def test_routine_restart_never_reimports_and_keeps_manual_bindings(n8n: FakeN8n):
    before = n8n.stored_raw(GMAIL_ID)
    out = n8n.boot()

    assert n8n.imports() == []
    assert n8n.stored_raw(GMAIL_ID) == before
    assert "preserving existing configuration and credential bindings" in out


def test_forced_gmail_update_leaves_website_workflow_unchanged(n8n: FakeN8n):
    website_before = n8n.stored(WEBSITE_ID)
    n8n.change_repo_gmail_workflow()

    n8n.boot(N8N_FORCE_IMPORT_GMAIL_WORKFLOW="true")

    assert n8n.imports() == [GMAIL_ID]
    website_after = n8n.stored(WEBSITE_ID)
    assert website_after["nodes"] == website_before["nodes"]
    assert website_after["versionId"] == website_before["versionId"]
    assert website_after["activeVersionId"] == website_before["activeVersionId"]


def test_forced_gmail_update_keeps_bindings_and_stays_inactive(n8n: FakeN8n):
    n8n.change_repo_gmail_workflow()

    out = n8n.boot(N8N_FORCE_IMPORT_GMAIL_WORKFLOW="true")

    gmail = n8n.stored(GMAIL_ID)
    assert gmail["name"].endswith("(next release)"), "repository content was imported"
    assert _gmail_bindings(gmail) == {"gmail": "uiBoundGmail01", "api": "uiBoundApi0002", "label": ["Label_9001"]}
    assert gmail["active"] is False and gmail["activeVersionId"] is None
    assert "publish:workflow --id=gmail-lead-intake" not in n8n.calls()
    assert "imported inactive" in out


def test_force_flag_left_set_does_not_overwrite_on_later_restarts(n8n: FakeN8n):
    n8n.change_repo_gmail_workflow()
    n8n.boot(N8N_FORCE_IMPORT_GMAIL_WORKFLOW="true")
    after_release = n8n.stored_raw(GMAIL_ID)

    for _ in range(2):
        out = n8n.boot(N8N_FORCE_IMPORT_GMAIL_WORKFLOW="true")
        assert n8n.imports() == []
        assert "Remove N8N_FORCE_IMPORT_GMAIL_WORKFLOW" in out
    assert n8n.stored_raw(GMAIL_ID) == after_release


def test_forced_import_is_refused_while_gmail_workflow_is_active(n8n: FakeN8n):
    gmail = n8n.stored(GMAIL_ID)
    gmail["activeVersionId"] = gmail["versionId"]
    (n8n.db / f"{GMAIL_ID}.json").write_text(json.dumps(gmail))
    before = n8n.stored_raw(GMAIL_ID)
    n8n.change_repo_gmail_workflow()

    out = n8n.boot(N8N_FORCE_IMPORT_GMAIL_WORKFLOW="true")

    assert n8n.imports() == []
    assert n8n.stored_raw(GMAIL_ID) == before
    assert "ERROR: forced Gmail import refused" in out


def test_website_force_flag_does_not_touch_gmail_workflow(n8n: FakeN8n):
    before = n8n.stored_raw(GMAIL_ID)
    n8n.change_repo_gmail_workflow()

    n8n.boot(N8N_FORCE_IMPORT_WORKFLOW="true")

    assert GMAIL_ID not in n8n.imports()
    assert n8n.stored_raw(GMAIL_ID) == before


def test_unreadable_database_is_not_mistaken_for_missing_workflow(n8n: FakeN8n):
    out = n8n.boot(FAKE_N8N_DB_DOWN="1", N8N_FORCE_IMPORT_GMAIL_WORKFLOW="true")

    assert not any(c.startswith("import:workflow") and "gmail" in c for c in n8n.calls())
    assert "ERROR: could not read gmail-lead-intake" in out
    assert n8n.calls()[-1] == "start"


def test_failed_forced_import_is_visible_and_n8n_still_starts(n8n: FakeN8n):
    before = n8n.stored_raw(GMAIL_ID)
    n8n.change_repo_gmail_workflow()

    out = n8n.boot(N8N_FORCE_IMPORT_GMAIL_WORKFLOW="true", FAKE_N8N_IMPORT_FAIL="1")

    assert "ERROR: gmail-lead-intake import failed" in out
    assert n8n.stored_raw(GMAIL_ID) == before
    assert n8n.calls()[-1] == "start"


def test_dockerfile_ships_the_gmail_import_helper():
    dockerfile = (REPO_ROOT / "deploy" / "docker" / "Dockerfile.n8n").read_text(encoding="utf-8")
    assert "COPY deploy/docker/gmail-workflow-import.js /workflows/gmail-workflow-import.js" in dockerfile
    assert "GMAIL_HELPER=/workflows/gmail-workflow-import.js" in ENTRYPOINT.read_text(encoding="utf-8")


def test_repository_workflows_carry_no_credential_secrets():
    """Credential references are {id, name} placeholders; secrets live only in n8n."""
    secret_keys = {"clientsecret", "client_secret", "accesstoken", "access_token",
                   "refreshtoken", "refresh_token", "oauthtokendata", "value", "password"}
    for name in ("gmail-lead-intake.json", "lead-intake.json"):
        workflow = json.loads((REPO_ROOT / "n8n" / name).read_text(encoding="utf-8"))
        for node in workflow["nodes"]:
            for ref in node.get("credentials", {}).values():
                assert set(ref) <= {"id", "name"}, f"{name}:{node['name']} credential has extra fields"
        text = json.dumps(workflow).lower()
        for key in secret_keys - {"value", "password"}:
            assert f'"{key}"' not in text, f"{name} contains {key}"
    gmail = json.loads((REPO_ROOT / "n8n" / "gmail-lead-intake.json").read_text(encoding="utf-8"))
    refs = [r["id"] for n in gmail["nodes"] for r in n.get("credentials", {}).values()]
    assert sorted(refs) == ["fastapi_auth_token", "gmail_oauth_pilot"], "placeholders only"
    assert "leadintelSourceSha256" not in json.dumps(gmail), "the hash is stamped at import time"
