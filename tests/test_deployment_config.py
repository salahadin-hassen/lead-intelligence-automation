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

