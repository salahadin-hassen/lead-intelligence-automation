"""Turnstile in the lead form (Task 05B): widget gate, token lifecycle, requests.

The page's TURNSTILE_GATE and INTAKE_REQUEST blocks are taken verbatim from
demo/lead-form.html and run in Node.js against a mocked Turnstile API, mocked
fetch and manual timers. No Cloudflare request is ever made.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO_ROOT / "demo" / "lead-form.html"

PAYLOAD = {
    "name": "Jamie Example",
    "email": "jamie@example.com",
    "company": "Example Systems",
    "message": "We are evaluating a demo and pricing.",
    "source": "website-demo-contact-form",
    "external_id": "lead-ext-1",
    "demo_insights": True,
}

HARNESS = r"""
const [blocks, payloadJson, scenario] = [process.argv[1], process.argv[2], process.argv[3]];
const lib = new Function(blocks + `; return { createTurnstileGate, submitIntake, buildWebhookBody,
  classifyIntakeResponse, TURNSTILE_ACTION, TURNSTILE_TOKEN_WAIT_MS, INTAKE_TIMEOUT_MS,
  VERIFICATION_FAILED_MESSAGE, VERIFICATION_UNAVAILABLE_MESSAGE, VERIFICATION_MISSING_MESSAGE };`)();
const PAYLOAD = JSON.parse(payloadJson);

function fakeApi() {
  const api = { renders: [], resets: [], opts: null,
    render(container, opts) { this.renders.push({ container, opts }); this.opts = opts; return "widget-1"; },
    reset(id) { this.resets.push(id); } };
  return api;
}
function fakeTimers() {
  const pending = new Map(); let next = 1;
  return {
    set: (fn, ms) => { const id = next++; pending.set(id, { fn, ms }); return id; },
    clear: (id) => { pending.delete(id); },
    fireAll: () => { const all = [...pending.values()]; pending.clear(); all.forEach((t) => t.fn()); },
    count: () => pending.size,
  };
}
function makeGate(api, timers, extra) {
  const states = [];
  const gate = lib.createTurnstileGate(Object.assign({
    siteKey: "public-site-key", container: "#turnstile-widget",
    getApi: () => api, onState: (s) => states.push(s), setTimer: timers.set, clearTimer: timers.clear,
  }, extra || {}));
  return { gate, states };
}
function fakeFetch(responses) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return { status: next.status, json: async () => { if (next.data === undefined) throw new Error("no json"); return next.data; } };
  };
  return { impl, calls };
}
const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  const out = await (new Function("lib", "PAYLOAD", "fakeApi", "fakeTimers", "makeGate", "fakeFetch", "tick",
    "return (async () => {" + scenario + "})();"))(lib, PAYLOAD, fakeApi, fakeTimers, makeGate, fakeFetch, tick);
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def _page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    page = _page()
    return page[page.index(start):page.index(end)]


def _blocks() -> str:
    return _block("/* TURNSTILE_GATE_START", "/* TURNSTILE_GATE_END */") + "\n" + _block(
        "/* INTAKE_REQUEST_START", "/* INTAKE_REQUEST_END */"
    )


def run(scenario: str):
    res = subprocess.run(
        ["node", "-e", HARNESS, _blocks(), json.dumps(PAYLOAD), scenario], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


# --- 1. widget initialisation --------------------------------------------------


def test_widget_renders_once_with_public_site_key_and_expected_action():
    out = run("""
      const api = fakeApi(), t = fakeTimers(); const { gate, states } = makeGate(api, t);
      const first = gate.render(), second = gate.render();
      const o = api.renders[0].opts;
      return { first, second, renders: api.renders.length, container: api.renders[0].container,
               sitekey: o.sitekey, action: o.action, refresh: o["refresh-expired"],
               callbacks: ["callback", "expired-callback", "timeout-callback", "error-callback"].every((k) => typeof o[k] === "function"),
               state: gate.state(), states };
    """)
    assert out == {
        "first": True, "second": True, "renders": 1, "container": "#turnstile-widget",
        "sitekey": "public-site-key", "action": "lead-intake", "refresh": "auto",
        "callbacks": True, "state": "pending", "states": ["pending"],
    }


def test_render_waits_for_script_then_renders_when_view_becomes_visible():
    out = run("""
      let api = undefined; const t = fakeTimers();
      const { gate } = makeGate(null, t, { getApi: () => api });
      const before = gate.render(); const stateBefore = gate.state();
      api = fakeApi(); const after = gate.render();
      return { before, stateBefore, after, renders: api.renders.length };
    """)
    assert out == {"before": False, "stateBefore": "loading", "after": True, "renders": 1}


@pytest.mark.parametrize(
    "setup",
    [
        "const { gate } = makeGate(api, t, { siteKey: '' }); gate.render();",  # no site key configured
        "const { gate } = makeGate(api, t); gate.markUnavailable();",  # api.js failed to load
        "api.render = () => { throw new Error('bad sitekey'); }; const { gate } = makeGate(api, t); gate.render();",
    ],
    ids=["unconfigured", "script-failed", "render-threw"],
)
def test_permanent_widget_failure_fails_closed_without_any_request(setup):
    out = run(f"""
      const api = fakeApi(), t = fakeTimers(); {setup}
      const f = fakeFetch([{{ status: 201, data: {{}} }}]);
      const result = await lib.submitIntake({{ url: "/webhook/lead-intake", payload: PAYLOAD, gate,
        tokenWaitMs: 15000, timeoutMs: 120000, fetchImpl: f.impl, setTimer: t.set, clearTimer: t.clear }});
      return {{ kind: result.kind, fetches: f.calls.length, state: gate.state(), timers: t.count() }};
    """)
    assert out["kind"] == "verification_missing"
    assert out["fetches"] == 0
    assert out["state"] in ("unconfigured", "unavailable")
    assert out["timers"] == 0  # answered immediately, not after the wait


def test_script_failure_releases_a_submission_already_waiting():
    out = run("""
      const api = fakeApi(), t = fakeTimers(); const { gate } = makeGate(api, t); gate.render();
      const waiting = gate.takeToken(15000); gate.markUnavailable();
      return { token: await waiting };
    """)
    assert out == {"token": None}


# --- 2, 6. token acquisition, single use, expiry -------------------------------


def test_token_is_handed_out_once_and_widget_is_reset_for_a_fresh_one():
    out = run("""
      const api = fakeApi(), t = fakeTimers(); const { gate, states } = makeGate(api, t); gate.render();
      api.opts.callback("tok-1");
      const ready = gate.state(); const first = await gate.takeToken(15000);
      const afterTake = { state: gate.state(), resets: api.resets.slice(), has: gate.hasToken() };
      const second = gate.takeToken(15000); t.fireAll();  // no new token arrives: wait expires
      return { ready, first, afterTake, second: await second, states };
    """)
    assert out["ready"] == "ready"
    assert out["first"] == "tok-1"
    assert out["afterTake"] == {"state": "pending", "resets": ["widget-1"], "has": False}
    assert out["second"] is None  # a consumed token is never handed out again


def test_submission_waits_for_a_token_that_arrives_later():
    out = run("""
      const api = fakeApi(), t = fakeTimers(); const { gate } = makeGate(api, t); gate.render();
      const waiting = gate.takeToken(15000); await tick(); api.opts.callback("tok-late");
      return { token: await waiting, timers: t.count() };
    """)
    assert out == {"token": "tok-late", "timers": 0}


@pytest.mark.parametrize("event", ["expired-callback", "timeout-callback"])
def test_expired_token_is_dropped_and_replaced_by_the_refreshed_one(event):
    out = run(f"""
      const api = fakeApi(), t = fakeTimers(); const {{ gate }} = makeGate(api, t); gate.render();
      api.opts.callback("tok-old"); api.opts["{event}"]();
      const state = gate.state(), has = gate.hasToken();
      const waiting = gate.takeToken(15000); await tick(); api.opts.callback("tok-new");
      return {{ state, has, token: await waiting }};
    """)
    assert out == {"state": "expired", "has": False, "token": "tok-new"}


def test_error_callback_clears_token_and_can_recover():
    out = run("""
      const api = fakeApi(), t = fakeTimers(); const { gate } = makeGate(api, t); gate.render();
      api.opts.callback("tok-1"); api.opts["error-callback"]();
      const state = gate.state(), has = gate.hasToken();
      const waiting = gate.takeToken(15000); await tick(); api.opts.callback("tok-2");
      return { state, has, token: await waiting };
    """)
    assert out == {"state": "error", "has": False, "token": "tok-2"}


def test_empty_or_non_string_callback_values_are_not_tokens():
    out = run("""
      const api = fakeApi(), t = fakeTimers(); const { gate } = makeGate(api, t); gate.render();
      api.opts.callback(""); api.opts.callback(null); api.opts.callback(42);
      const waiting = gate.takeToken(15000); t.fireAll();
      return { has: gate.hasToken(), token: await waiting };
    """)
    assert out == {"has": False, "token": None}


# --- 3, 4, 5. request body --------------------------------------------------------


def test_token_is_sent_beside_the_lead_fields_and_payload_is_not_mutated():
    out = run("""
      const api = fakeApi(), t = fakeTimers(); const { gate } = makeGate(api, t); gate.render();
      api.opts.callback("tok-1");
      const before = JSON.stringify(PAYLOAD);
      const f = fakeFetch([{ status: 201, data: { outcome: "created", lead: { lead_id: 7 } } }]);
      const result = await lib.submitIntake({ url: "/webhook/lead-intake", payload: PAYLOAD, gate,
        tokenWaitMs: 15000, timeoutMs: 120000, fetchImpl: f.impl, setTimer: t.set, clearTimer: t.clear });
      const c = f.calls[0];
      return { kind: result.kind, url: c.url, method: c.init.method, headers: c.init.headers, body: c.body,
               unchanged: JSON.stringify(PAYLOAD) === before, timers: t.count() };
    """)
    assert out["kind"] == "created"
    assert out["url"] == "/webhook/lead-intake" and out["method"] == "POST"
    assert out["headers"] == {"Content-Type": "application/json"}  # token is not a browser header
    assert out["body"] == {**PAYLOAD, "turnstile_token": "tok-1"}
    assert out["unchanged"] is True
    assert out["timers"] == 0


def test_missing_token_after_wait_prevents_submission():
    out = run("""
      const api = fakeApi(), t = fakeTimers(); const { gate } = makeGate(api, t); gate.render();
      const f = fakeFetch([{ status: 201, data: {} }]);
      const pending = lib.submitIntake({ url: "/w", payload: PAYLOAD, gate, tokenWaitMs: 15000,
        timeoutMs: 120000, fetchImpl: f.impl, setTimer: t.set, clearTimer: t.clear });
      await tick(); t.fireAll();
      return { kind: (await pending).kind, fetches: f.calls.length, wait: lib.TURNSTILE_TOKEN_WAIT_MS };
    """)
    assert out == {"kind": "verification_missing", "fetches": 0, "wait": 15000}


def test_explicit_development_bypass_sends_no_token():
    out = run("""
      const f = fakeFetch([{ status: 201, data: { outcome: "created", lead: {} } }]);
      const r = await lib.submitIntake({ url: "/w", payload: PAYLOAD, gate: null, timeoutMs: 1000, fetchImpl: f.impl });
      return { kind: r.kind, hasToken: "turnstile_token" in f.calls[0].body };
    """)
    assert out == {"kind": "created", "hasToken": False}


# --- 7, 8, 12. response handling -------------------------------------------------


@pytest.mark.parametrize(
    ("status", "data", "kind"),
    [
        (201, {"outcome": "created", "lead": {"lead_id": 1}}, "created"),
        (201, {"outcome": "created"}, "other"),  # unchanged: no lead object
        (409, {"outcome": "duplicate"}, "duplicate"),
        (200, {"outcome": "duplicate"}, "duplicate"),
        (422, {"outcome": "validation_failed"}, "validation_failed"),
        (403, {"outcome": "verification_failed"}, "verification_failed"),
        (403, None, "verification_failed"),
        (503, {"outcome": "verification_unavailable"}, "verification_unavailable"),
        (503, None, "verification_unavailable"),
        (502, {"outcome": "upstream_error"}, "upstream_error"),
        (500, None, "other"),
    ],
)
def test_response_classification(status, data, kind):
    out = run(f"return lib.classifyIntakeResponse({status}, {json.dumps(data)});")
    assert out == kind


def test_verification_messages_match_the_specified_copy():
    out = run("return [lib.VERIFICATION_FAILED_MESSAGE, lib.VERIFICATION_UNAVAILABLE_MESSAGE];")
    assert out == [
        "Verification failed. Complete the verification and try again.",
        "Verification service is temporarily unavailable. Please try again shortly.",
    ]


# --- 9, 10. retries: fresh token, same external_id -------------------------------


@pytest.mark.parametrize(
    ("first", "second", "kinds"),
    [
        ("{ status: 403, data: { outcome: 'verification_failed' } }", "{ status: 201, data: { outcome: 'created', lead: {} } }",
         ["verification_failed", "created"]),
        ("{ status: 503, data: { outcome: 'verification_unavailable' } }", "{ status: 201, data: { outcome: 'created', lead: {} } }",
         ["verification_unavailable", "created"]),
        # Response lost after the lead was stored: the retry is answered 409, not a second lead.
        ("new Error('network down')", "{ status: 409, data: { outcome: 'duplicate' } }", ["network_error", "duplicate"]),
    ],
    ids=["after-403", "after-503", "after-network-error"],
)
def test_retry_uses_a_fresh_token_and_the_same_external_id(first, second, kinds):
    out = run(f"""
      const api = fakeApi(), t = fakeTimers(); const {{ gate }} = makeGate(api, t); gate.render();
      const f = fakeFetch([{first}, {second}]);
      const opts = {{ url: "/w", payload: PAYLOAD, gate, tokenWaitMs: 15000, timeoutMs: 120000,
                      fetchImpl: f.impl, setTimer: t.set, clearTimer: t.clear }};
      api.opts.callback("tok-1"); const r1 = await lib.submitIntake(opts);
      const retryWithoutNewToken = lib.submitIntake(opts); await tick();
      api.opts.callback("tok-2"); const r2 = await retryWithoutNewToken;
      return {{ kinds: [r1.kind, r2.kind], tokens: f.calls.map((c) => c.body.turnstile_token),
               ids: f.calls.map((c) => c.body.external_id), resets: api.resets.length }};
    """)
    assert out["kinds"] == kinds
    assert out["tokens"] == ["tok-1", "tok-2"]
    assert out["ids"] == ["lead-ext-1", "lead-ext-1"]
    assert out["resets"] == 2


def test_request_timeout_aborts_and_reports_a_network_error():
    out = run("""
      const t = fakeTimers();
      const fetchImpl = (url, init) => new Promise((_, reject) => {
        init.signal.addEventListener("abort", () => reject(new Error("aborted")));
      });
      const pending = lib.submitIntake({ url: "/w", payload: PAYLOAD, gate: null, timeoutMs: 120000,
        fetchImpl, setTimer: t.set, clearTimer: t.clear });
      await tick(); t.fireAll();
      const r = await pending;
      return { kind: r.kind, timeout: lib.INTAKE_TIMEOUT_MS };
    """)
    assert out == {"kind": "network_error", "timeout": 120000}


# --- page wiring (static checks on the real page) --------------------------------


def _handle_submit() -> str:
    page = _page()
    start = page.index("async function handleSubmit(event)")
    return page[start:page.index('form.addEventListener("submit", handleSubmit);', start)]


def test_page_submits_only_through_the_gated_request_helper():
    handler = _handle_submit()
    assert "fetch(" not in handler, "handleSubmit must not call fetch directly"
    assert "submitIntake({" in handler and "gate: turnstileGate" in handler
    assert "lastSubmittedPayload = Object.assign({}, payload);" in handler  # stored copy has no token
    assert "turnstile_token" not in handler
    assert handler.index("verification_missing") < handler.index('result.kind === "created"')
    for message in ("VERIFICATION_FAILED_MESSAGE", "VERIFICATION_UNAVAILABLE_MESSAGE", "VERIFICATION_MISSING_MESSAGE"):
        assert message in handler
    # Existing messages are preserved.
    assert "The intake service could not complete this request. Please try again shortly." in handler
    assert "No response was received from the intake service." in handler


def test_page_loads_turnstile_explicitly_and_renders_on_the_demo_view():
    page = _page()
    assert 'src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"' in page
    assert "turnstileScript.onerror = function() { turnstileGate.markUnavailable(); };" in page
    assert 'if (isDemo) ensureTurnstileRendered();' in page
    form = page[page.index('<form id="lead-form"'):page.index("</form>")]
    assert form.index('id="turnstile-widget"') < form.index('id="submit-btn"')
    assert 'id="turnstile-status" aria-live="polite"' in form


def test_development_bypass_is_limited_to_localhost():
    page = _page()
    assert re.search(
        r"const TURNSTILE_BYPASS = CONFIG\.turnstileDisabled &&\s*\(window\.location\.hostname === \"localhost\" \|\| "
        r"window\.location\.hostname === \"127\.0\.0\.1\"\);",
        page,
    )
    assert "turnstileDisabled: (window.LEAD_FORM_CONFIG || {}).turnstileDisabled === true" in page


def test_page_contains_no_turnstile_secret():
    page = _page().lower()
    assert "secret" not in page.replace("never the secret", "").replace("the secret stays on the api", "")
    assert "siteverify" not in page


# --- error banner visibility (Task 07C) ------------------------------------------
# clearAllErrors() used to leave an inline display:none on #banner, which
# overrides ".form-banner.error { display: block }": every error that is shown
# only in the banner (missing token, 403, 503, validation) stayed invisible.


def test_clear_all_errors_leaves_no_inline_style_on_the_banner():
    block = _block("/* CLEAR_ERRORS_START", "/* CLEAR_ERRORS_END */")
    script = r"""
      const block = process.argv[1];
      const attrs = { style: "display: block; color: orange" };
      const banner = { textContent: "old notice", className: "form-banner error", style: { display: "block" },
        removeAttribute(name) { delete attrs[name]; if (name === "style") this.style = {}; } };
      const FIELDS = ["name"];
      const document = { getElementById: () => null };
      new Function("banner", "FIELDS", "document", block + "; clearAllErrors();")(banner, FIELDS, document);
      console.log(JSON.stringify({ text: banner.textContent, cls: banner.className,
        hasStyleAttr: "style" in attrs, inlineDisplay: banner.style.display || null }));
    """
    res = subprocess.run(["node", "-e", script, block], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {"text": "", "cls": "form-banner", "hasStyleAttr": False, "inlineDisplay": None}


def test_page_never_hides_the_banner_with_an_inline_display_none():
    page = _page()
    assert not re.search(r"banner\.style\.display\s*=\s*[\"']none[\"']", page)
    # Visibility stays with the CSS classes.
    assert re.search(r"\.form-banner \{\s*display: none;", page)
    assert re.search(r"\.form-banner\.error \{\s*display: block;", page)


def _chrome() -> str | None:
    import shutil

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


# The real page with only two things replaced: the Turnstile script (a stub API
# that either issues a token or never does) and window.fetch (a stub returning
# the scenario's response). Nothing leaves the machine.
_BROWSER_STUB = r"""<script>
window.LEAD_FORM_CONFIG = { webhookUrl: "/webhook/lead-intake", turnstileSiteKey: "public-site-key" };
window.__fetches = [];
window.__scenario = __SCENARIO__;
window.fetch = function(url, init) {
  window.__fetches.push({ url: url, method: init.method, body: JSON.parse(init.body) });
  const r = window.__scenario.response;
  return Promise.resolve({ status: r.status, json: function() { return Promise.resolve(r.data); } });
};
window.turnstile = {
  render: function(el, opts) {
    if (window.__scenario.token) setTimeout(function() { opts.callback("stub-token-" + "x".repeat(16)); }, 100);
    return "widget-1";
  },
  reset: function() {},
};
</script>"""

_BROWSER_PROBE = r"""<script>
window.addEventListener("load", function() { setTimeout(function() {
  const set = function(id, v) { document.getElementById(id).value = v; };
  if (window.__scenario.duplicatePreset) document.getElementById("preset-duplicate").click();
  set("name", "Synthetic Test Lead"); set("email", "e2e-turnstile-check@example.com");
  set("company", "Example Synthetic Org");
  set("message", "Synthetic end-to-end verification submission. Please disregard this record.");
  document.getElementById("lead-form").requestSubmit();
  setTimeout(function() {
    const b = document.getElementById("banner"), cs = getComputedStyle(b), rect = b.getBoundingClientRect();
    const probe = document.createElement("div"); probe.className = "form-banner error";
    b.parentNode.insertBefore(probe, b); const errorColor = getComputedStyle(probe).color; probe.remove();
    const out = {
      bannerText: b.textContent, bannerClass: b.className, bannerDisplay: cs.display,
      bannerHeight: Math.round(rect.height), bannerInlineDeclarations: b.style.length,
      bannerColorIsErrorColor: cs.color === errorColor,
      outcomeVisible: document.getElementById("outcome-panel").classList.contains("visible"),
      submitDisabled: document.getElementById("submit-btn").disabled,
      fetches: window.__fetches.map(function(f) { return { url: f.url, method: f.method, keys: Object.keys(f.body).sort() }; }),
    };
    const pre = document.createElement("pre"); pre.id = "RESULT"; pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }, 17000);
}, 300); });
</script></body>"""


def _run_page_in_browser(tmp_path: Path, scenario: dict) -> dict:
    import html
    import os

    chrome = _chrome()
    if chrome is None:
        pytest.skip("Chrome/Chromium is not installed")
    page = _page()
    stub = _BROWSER_STUB.replace("__SCENARIO__", json.dumps(scenario))
    assert page.count('<script src="/config.js"></script>') == 1
    page = page.replace('<script src="/config.js"></script>', stub, 1)
    api_url = '"https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"'
    assert page.count(api_url) == 1
    page = page.replace(api_url, '"data:text/javascript,"', 1)
    page = page.replace("</body>", _BROWSER_PROBE, 1)
    page_file = tmp_path / "index.html"
    page_file.write_text(page, encoding="utf-8")
    args = [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={tmp_path / 'profile'}", "--window-size=1366,768",
            "--virtual-time-budget=30000", "--dump-dom", page_file.as_uri() + "?view=demo"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")
    res = subprocess.run(args, capture_output=True, text=True, timeout=120)
    match = re.search(r'<pre id="RESULT">(.*?)</pre>', res.stdout, re.S)
    assert match, f"page probe produced no result (exit {res.returncode}): {res.stderr[-2000:]}"
    return json.loads(html.unescape(match.group(1)))


def _assert_visible_error(out: dict, message: str) -> None:
    assert out["bannerText"] == message
    assert out["bannerClass"] == "form-banner error"
    assert out["bannerDisplay"] == "block"
    assert out["bannerHeight"] > 0
    # No inline declarations at all (Chromium may serialise that as no attribute
    # or as an empty style="" — both mean nothing overrides the CSS classes).
    assert out["bannerInlineDeclarations"] == 0
    assert out["bannerColorIsErrorColor"] is True
    assert out["submitDisabled"] is False


def test_browser_missing_token_error_is_visible_and_nothing_is_sent(tmp_path):
    out = _run_page_in_browser(tmp_path, {"token": False, "response": {"status": 201, "data": {}}})
    _assert_visible_error(out, "Verification is not complete yet. Complete the verification and try again.")
    assert out["fetches"] == []
    assert out["outcomeVisible"] is False


@pytest.mark.parametrize(
    ("status", "outcome", "message"),
    [
        (403, "verification_failed", "Verification failed. Complete the verification and try again."),
        (503, "verification_unavailable",
         "Verification service is temporarily unavailable. Please try again shortly."),
    ],
)
def test_browser_verification_errors_are_visible(tmp_path, status, outcome, message):
    data = {"outcome": outcome, "message": message}
    out = _run_page_in_browser(tmp_path, {"token": True, "response": {"status": status, "data": data}})
    _assert_visible_error(out, message)
    assert len(out["fetches"]) == 1
    assert out["outcomeVisible"] is False


def test_browser_error_after_duplicate_preset_uses_error_styling(tmp_path):
    # The duplicate-test preset leaves inline notice colours on the banner; a
    # later error must not inherit them (or an inline display).
    data = {"outcome": "verification_failed", "message": "x"}
    out = _run_page_in_browser(
        tmp_path, {"token": True, "duplicatePreset": True, "response": {"status": 403, "data": data}}
    )
    _assert_visible_error(out, "Verification failed. Complete the verification and try again.")


def test_browser_successful_submission_is_unchanged(tmp_path):
    lead = {"lead_id": 101, "name": "Synthetic Test Lead", "email": "e2e-turnstile-check@example.com",
            "company": "Example Synthetic Org", "source": "website-demo-contact-form", "status": "new",
            "created_at": "2026-11-17T12:00:00Z", "score": 63,
            "score_reason": "corporate email domain; strong source (contact form)"}
    data = {"outcome": "created", "apiStatus": 201, "lead": lead, "score": 63, "score_reason": lead["score_reason"]}
    out = _run_page_in_browser(tmp_path, {"token": True, "response": {"status": 201, "data": data}})
    assert out["outcomeVisible"] is True
    assert out["bannerText"] == ""
    assert out["bannerDisplay"] == "none"
    assert out["submitDisabled"] is False
    assert out["fetches"] == [{
        "url": "/webhook/lead-intake", "method": "POST",
        "keys": ["company", "demo_insights", "email", "external_id", "message", "name", "source", "turnstile_token"],
    }]
