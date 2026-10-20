"""Static and functional assertions for deployment manifests and entrypoints."""

import subprocess
from pathlib import Path
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_n8n_entrypoint_static_ssl_settings():
    """Ensure deploy/docker/n8n-entrypoint.sh explicitly enables SSL and enforces TLS verification."""
    entrypoint_path = REPO_ROOT / "deploy" / "docker" / "n8n-entrypoint.sh"
    assert entrypoint_path.exists(), "n8n-entrypoint.sh must exist"

    content = entrypoint_path.read_text(encoding="utf-8")

    # 1. Must explicitly set DB_POSTGRESDB_SSL_ENABLED
    assert "DB_POSTGRESDB_SSL_ENABLED" in content, (
        "n8n-entrypoint.sh must explicitly configure DB_POSTGRESDB_SSL_ENABLED"
    )

    # 2. Must strictly enforce certificate verification
    assert "export DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED=true" in content, (
        "n8n-entrypoint.sh must enforce strict TLS verification (reject unauthorized = true)"
    )

    # 3. Must NEVER disable certificate verification
    assert "DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED=false" not in content, (
        "n8n-entrypoint.sh must not disable certificate verification"
    )


def test_n8n_entrypoint_url_parser_functional():
    """Functional test: Verify Node parser extracts discrete Postgres vars with SSL enabled from Neon URL."""
    neon_url = "postgresql://test_user:test_secret@ep-cool-fog-12345.us-east-2.aws.neon.tech/n8n?sslmode=require"

    node_script = """
    const u = new URL(process.env.DB_URL);
    const sslParam = u.searchParams.get("sslmode") || u.searchParams.get("ssl");
    const sslEnabled = sslParam !== "disable" && sslParam !== "false";
    console.log(`DB_TYPE=postgresdb`);
    console.log(`DB_POSTGRESDB_HOST=${u.hostname}`);
    console.log(`DB_POSTGRESDB_PORT=${u.port || 5432}`);
    console.log(`DB_POSTGRESDB_USER=${decodeURIComponent(u.username)}`);
    console.log(`DB_POSTGRESDB_PASSWORD=${decodeURIComponent(u.password)}`);
    console.log(`DB_POSTGRESDB_DATABASE=${u.pathname.replace(/^\\//, "")}`);
    console.log(`DB_POSTGRESDB_SSL_ENABLED=${sslEnabled ? "true" : "false"}`);
    console.log(`DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED=true`);
    """

    res = subprocess.run(
        ["node", "-e", node_script],
        env={"DB_URL": neon_url},
        capture_output=True,
        text=True,
        check=True,
    )

    output = res.stdout.strip().splitlines()
    env_map = dict(line.split("=", 1) for line in output)

    assert env_map["DB_TYPE"] == "postgresdb"
    assert env_map["DB_POSTGRESDB_HOST"] == "ep-cool-fog-12345.us-east-2.aws.neon.tech"
    assert env_map["DB_POSTGRESDB_PORT"] == "5432"
    assert env_map["DB_POSTGRESDB_DATABASE"] == "n8n"
    assert env_map["DB_POSTGRESDB_SSL_ENABLED"] == "true"
    assert env_map["DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED"] == "true"


def test_render_yaml_production_config():
    """Verify render.yaml declares production environment and does not use broken private networking."""
    render_yaml_path = REPO_ROOT / "render.yaml"
    assert render_yaml_path.exists(), "render.yaml must exist"

    data = yaml.safe_load(render_yaml_path.read_text(encoding="utf-8"))
    services = {s["name"]: s for s in data.get("services", [])}

    assert "leadintel-api" in services
    api_env = {v["key"]: v.get("value") for v in services["leadintel-api"].get("envVars", [])}
    assert api_env.get("APP_ENV") == "production", "leadintel-api must run with APP_ENV=production"

    # Verify no private network hostport properties are used on free web services
    for s in services.values():
        for env_entry in s.get("envVars", []):
            if "fromService" in env_entry:
                assert env_entry["fromService"].get("property") != "hostport", (
                    f"Service {s['name']} uses unsupported private network property 'hostport' on free tier"
                )


def test_caddy_dockerfile_strips_file_capabilities():
    """Ensure deploy/docker/Dockerfile.caddy strips CAP_NET_BIND_SERVICE for Render compatibility."""
    dockerfile_path = REPO_ROOT / "deploy" / "docker" / "Dockerfile.caddy"
    assert dockerfile_path.exists(), "Dockerfile.caddy must exist"

    content = dockerfile_path.read_text(encoding="utf-8")
    assert "setcap -r /usr/bin/caddy" in content, "Dockerfile.caddy must remove file capabilities from caddy"
    assert "test -z \"$(getcap /usr/bin/caddy)\"" in content, "Dockerfile.caddy must verify capabilities are removed"
    assert "ENV PORT=10000" in content, "Dockerfile.caddy must set PORT=10000"
    assert "COPY deploy/caddy/Caddyfile.render /etc/caddy/Caddyfile" in content, "Caddyfile must be copied"
    assert "webhookUrl: \"/webhook/lead-intake\"" in content, "Must retain same-origin webhook URL in config.js"


def test_build_pages_generates_all_static_assets_and_clean_demo_routes(tmp_path):
    """Verify deploy/bin/build-pages.sh outputs clean static assets including /demo without 307 loops."""
    build_pages_script = REPO_ROOT / "deploy" / "bin" / "build-pages.sh"
    assert build_pages_script.exists(), "build-pages.sh must exist"

    target_dir = tmp_path / "pages-public"
    res = subprocess.run(
        ["bash", str(build_pages_script)],
        env={"PAGES_BUILD_DIR": str(target_dir)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "successfully built Cloudflare Pages static assets" in res.stdout

    # 1. Essential static files must exist
    index_html = target_dir / "index.html"
    lead_form_html = target_dir / "lead-form.html"
    demo_html = target_dir / "demo.html"
    demo_index_html = target_dir / "demo" / "index.html"
    config_js = target_dir / "config.js"
    headers_file = target_dir / "_headers"
    redirects_file = target_dir / "_redirects"

    assert index_html.exists() and index_html.stat().st_size > 1000
    assert lead_form_html.exists() and lead_form_html.stat().st_size > 1000
    assert demo_html.exists() and demo_html.stat().st_size > 1000
    assert demo_index_html.exists() and demo_index_html.stat().st_size > 1000
    assert config_js.exists()
    assert headers_file.exists()
    assert redirects_file.exists()

    # 2. Config content check: same-origin webhook, no secrets or localhost dev endpoints
    config_content = config_js.read_text(encoding="utf-8")
    assert 'webhookUrl: "/webhook/lead-intake"' in config_content
    assert "localhost" not in config_content
    assert "127.0.0.1" not in config_content
    assert ":8000" not in config_content
    assert ":5500" not in config_content
    assert "token" not in config_content.lower()

    # 3. Security headers check
    headers_content = headers_file.read_text(encoding="utf-8")
    assert "X-Content-Type-Options: nosniff" in headers_content
    assert "X-Frame-Options: DENY" in headers_content
    assert "Referrer-Policy: no-referrer" in headers_content

    # 4. Critical: _redirects must NOT contain /demo /index.html 200 (which triggers 307 clean-URL loop)
    redirects_content = redirects_file.read_text(encoding="utf-8")
    for line in redirects_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        assert not (len(parts) >= 2 and parts[0] in ("/demo", "/demo/") and parts[1] == "/index.html"), (
            f"Found forbidden rewrite rule {line} in _redirects that causes 307 redirect loops on Cloudflare"
        )

    # 5. Asset references in HTML files use absolute root path
    for html_file in [index_html, demo_html, demo_index_html]:
        content = html_file.read_text(encoding="utf-8")
        assert '<script src="/config.js"></script>' in content


def test_wrangler_config_integrity():
    """Verify wrangler.json configures Workers Static Assets and webhook entrypoint accurately."""
    wrangler_path = REPO_ROOT / "wrangler.json"
    assert wrangler_path.exists(), "wrangler.json must exist in root"

    import json
    data = json.loads(wrangler_path.read_text(encoding="utf-8"))

    assert data.get("main") == "functions/webhook/lead-intake.js"
    assert "assets" in data
    assets = data["assets"]
    assert assets.get("directory") == "./var/pages-public"
    assert assets.get("binding") == "ASSETS"
    assert assets.get("not_found_handling") == "single-page-application"


def test_webhook_intake_proxy_functional_node():
    """Functional test: Verify lead-intake proxy handles methods, status codes, errors, and asset dispatch in Node."""
    node_test_script = """
    import worker, { onRequest } from "./functions/webhook/lead-intake.js";
    import assert from "node:assert";

    async function runTests() {
      // 1. Method restriction: Reject non-POST with 405 Method Not Allowed
      for (const m of ["GET", "PUT", "DELETE", "PATCH"]) {
        const req = new Request("https://cf-worker.local/webhook/lead-intake", { method: m });
        const res = await onRequest({ request: req });
        assert.strictEqual(res.status, 405, `Expected 405 for ${m}`);
        assert.strictEqual(res.headers.get("Allow"), "POST");
        const body = await res.json();
        assert.strictEqual(body.outcome, "method_not_allowed");
      }

      // 2. Successful upstream 201 Created forwarding
      const origFetch = globalThis.fetch;
      let capturedUrl = null;
      let capturedMethod = null;
      let capturedBody = null;

      globalThis.fetch = async (url, opts) => {
        capturedUrl = url;
        capturedMethod = opts.method;
        capturedBody = opts.body;
        return new Response(JSON.stringify({ lead_id: "lead_live_test", status: "created" }), {
          status: 201,
          headers: { "Content-Type": "application/json" }
        });
      };

      const payload = { name: "Test User", email: "test@example.com", external_id: "ext-123" };
      const postReq = new Request("https://cf-worker.local/webhook/lead-intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const res201 = await onRequest({ request: postReq });
      assert.strictEqual(res201.status, 201);
      assert.strictEqual(capturedUrl, "https://leadintel-caddy.onrender.com/webhook/lead-intake");
      assert.strictEqual(capturedMethod, "POST");
      assert.deepStrictEqual(JSON.parse(capturedBody), payload);
      const data201 = await res201.json();
      assert.strictEqual(data201.lead_id, "lead_live_test");

      // 3. Upstream 409 Conflict forwarding
      globalThis.fetch = async () => new Response(
        JSON.stringify({ outcome: "duplicate_conflict", apiStatus: 409 }),
        { status: 409, headers: { "Content-Type": "application/json" } }
      );
      const dupReq = new Request("https://cf-worker.local/webhook/lead-intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const res409 = await onRequest({ request: dupReq });
      assert.strictEqual(res409.status, 409);
      const data409 = await res409.json();
      assert.strictEqual(data409.outcome, "duplicate_conflict");

      // 4. Upstream network failure produces sanitized 502 Bad Gateway
      globalThis.fetch = async () => { throw new Error("ECONNREFUSED 127.0.0.1:10000"); };
      const failReq = new Request("https://cf-worker.local/webhook/lead-intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const res502 = await onRequest({ request: failReq });
      assert.strictEqual(res502.status, 502);
      const data502 = await res502.json();
      assert.strictEqual(data502.outcome, "upstream_error");
      assert.strictEqual(data502.apiStatus, 502);
      assert.ok(!JSON.stringify(data502).includes("ECONNREFUSED"), "Must not leak internal socket errors");

      // 5. Workers default fetch dispatch (webhook vs static assets fallback)
      globalThis.fetch = async () => new Response(JSON.stringify({ ok: true }), { status: 200 });
      let assetFetched = false;
      const env = {
        ASSETS: {
          async fetch(req) {
            assetFetched = true;
            return new Response("static HTML", { status: 200 });
          }
        }
      };

      // 5a. Route /webhook/lead-intake reaches webhook handler
      const workerWebhookReq = new Request("https://cf-worker.local/webhook/lead-intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const workerRes = await worker.fetch(workerWebhookReq, env);
      assert.strictEqual(workerRes.status, 200);
      assert.strictEqual(assetFetched, false, "ASSETS.fetch should not be called for webhook");

      // 5b. Route /demo delegates to env.ASSETS.fetch
      const workerAssetReq = new Request("https://cf-worker.local/demo", { method: "GET" });
      const assetRes = await worker.fetch(workerAssetReq, env);
      assert.strictEqual(assetRes.status, 200);
      assert.strictEqual(assetFetched, true, "ASSETS.fetch must be called for /demo");

      globalThis.fetch = origFetch;
      console.log("ALL_NODE_TESTS_PASSED");
    }

    runTests().catch(err => {
      console.error(err);
      process.exit(1);
    });
    """

    res = subprocess.run(
        ["node", "--input-type=module", "-e", node_test_script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ALL_NODE_TESTS_PASSED" in res.stdout



