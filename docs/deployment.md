# Deployment (Milestone 12)

How the stack is deployed, configured and verified — and, just as important,
**exactly what was and was not verified** on this machine.

## Status

| Claim | Status |
|---|---|
| Deployment-ready configuration: scripts, systemd units, reverse-proxy config, env templates, docs | **yes** |
| Public deployment personally verified (real domain, issued TLS, systemd boot on a server) | **no** |

Everything marked *verified* below was executed on this machine against a
production-shaped local rehearsal (topology in
[Verification ledger](#verification-ledger)). No cloud host was provisioned,
no DNS record or TLS certificate was created, and this machine offers no
`sudo`/systemd boot — those portions are configuration-reviewed only and are
listed as unverified. The deployment-ready claim comes from running the whole
stack with production-like configuration, not from the files existing.

## Architecture

```
Internet
   │  HTTPS (Caddy, {$LEAD_DOMAIN}, automatic Let's Encrypt)
   ▼
Caddy :80/:443
   ├──  /             → rendered lead form (var/www, from demo/ + form.env)
   └──  /webhook/*    → reverse_proxy 127.0.0.1:5678 (path preserved)
                           ▼
                  n8n 127.0.0.1:5678 (loopback only, workflow engine)
                           │  POST {LEAD_API_BASE_URL}/leads
                           ▼
                  FastAPI 127.0.0.1:8000 ──→ PostgreSQL 127.0.0.1:5432
                           │
                           └ scoring: offline heuristic (default) or
                             OpenAI-compatible endpoint (optional)
                  n8n ──→ Telegram Bot API (outbound HTTPS, one chat)
```

Design decisions, chosen from this repo's reality rather than fashion:

- **Same-origin production form.** The form is served from the same domain
  that exposes `/webhook/*`, so production requests carry no cross-origin
  preflight and never depend on CORS. Local development remains cross-origin
  (`localhost:8001 → localhost:5679`) and is the only place CORS applies —
  see [CORS model](#cors-model).
- **Loopback everywhere.** `N8N_LISTEN_ADDRESS=127.0.0.1` and
  `API_HOST=127.0.0.1`: only the reverse proxy and local ops can reach them.
  The n8n editor is reached through an SSH tunnel
  (`ssh -L 5678:127.0.0.1:5678 <host>`), never exposed publicly.
- **Smallest sensible shape.** Two supervised processes (FastAPI, n8n), one
  reverse proxy (Caddy), one database (PostgreSQL 16). No containers, queue,
  Redis, workers or microservices. Docker was evaluated for this milestone
  and deliberately **not shipped**: no container runtime exists on the
  validation machine, and n8n 2.41.7 prints a deprecation warning when run
  outside a container (see [Known warnings](#known-warnings)).
- **Public URL configuration has exactly two places:** the form's
  `DEMO_N8N_WEBHOOK_URL` (rendered into `var/www/config.js`) and n8n's
  `N8N_WEBHOOK_URL`/`LEAD_API_BASE_URL` in the env files. Nothing else in
  the repo assumes `localhost` in production configuration.

| Component | Address | Reached by |
|---|---|---|
| Caddy | `:80/:443` (public) | browsers |
| n8n webhook | `127.0.0.1:5678` | Caddy (`/webhook/*`) |
| n8n editor/API | `127.0.0.1:5678` | SSH tunnel only |
| FastAPI | `127.0.0.1:8000` | n8n (`LEAD_API_BASE_URL`), local ops |
| PostgreSQL | `127.0.0.1:5432` | FastAPI only |
| n8n task-runner broker | `127.0.0.1:5679` | n8n internal (port gotcha: independent of `N8N_PORT`; move with `N8N_RUNNERS_BROKER_PORT` if taken) |

## What is in `deploy/`

| Path | Purpose |
|---|---|
| `deploy/env/api.env.example` | FastAPI config template (`DATABASE_URL`, optional AI vars, loopback listener) |
| `deploy/env/n8n.env.example` | n8n config template (bind address, public URLs, data folder, encryption key, workflow vars) |
| `deploy/env/form.env.example` | Form config template (`DEMO_N8N_WEBHOOK_URL` — the single webhook URL switch) |
| `deploy/bin/run-api.sh` | Deterministic uvicorn start (used by systemd and `stack.sh`) |
| `deploy/bin/run-n8n.sh` | Deterministic n8n start, optional pinned-version check (`LEAD_N8N_EXPECT_VERSION`) |
| `deploy/bin/import-workflow.sh` | **Offline** `import:workflow` + `publish:workflow` of `n8n/lead-intake.json` (idempotent upsert by `id`) |
| `deploy/bin/render-form.sh` | Renders `var/www/{index.html,lead-form.html,config.js}` from `demo/` + `form.env` |
| `deploy/bin/stack.sh` | No-root start/stop/restart/status/logs/render — the manual path and validation basis |
| `deploy/bin/migrate.sh` | Explicit run of the *same* `sql/` migrations the API applies at startup |
| `deploy/bin/healthcheck.sh` | Health probes with exit code (see [Health & smoke](#health--smoke-verification)) |
| `deploy/bin/smoke.sh` | End-to-end smoke test through the real webhook path |
| `deploy/bin/n8n_exec.py` | Read-only view into n8n's execution store (list/search executions) — the evidence tool used by the failure drills |
| `deploy/systemd/lead-api.service`, `lead-n8n.service` | Service units (same env files + run scripts as `stack.sh`) |
| `deploy/caddy/Caddyfile` | Production proxy config (`{$LEAD_DOMAIN}` + `{$LEAD_WWW_ROOT}`) |
| `deploy/caddy/Caddyfile.local` | Same routing shape on `:8080` without TLS — used for the rehearsal |

All scripts are bash (`bash -n` clean), deterministic, and fail loudly when a
required env file or tool is missing. `deploy/env/*.env` and `var/` are
gitignored; only `*.env.example` templates are tracked.

## Provisioning sequence (fresh host)

Prerequisites: PostgreSQL 16, Python 3.12, Node.js ≥ 20 with
`npm install -g n8n@2.41.7` (the version this workflow is validated against),
Caddy 2, git.

```bash
# 1. Code (the systemd units assume /opt/lead-intake)
git clone <repo> /opt/lead-intake && cd /opt/lead-intake
python3.12 -m venv .venv && .venv/bin/pip install -e .

# 2. Database — one role, one database (sql/ migrations are idempotent and
#    applied automatically at every API startup; deploy/bin/migrate.sh runs
#    them explicitly if you prefer a separate step)
sudo -u postgres createuser lead_app          # no createdb needed
sudo -u postgres createdb -O lead_app lead_automation

# 3. Service user (as commented in the units)
sudo useradd --system --home-dir /var/lib/lead-intake \
  --create-home --shell /usr/sbin/nologin leadintake
sudo chown -R leadintake:leadintake /var/lib/lead-intake

# 4. Configuration — templates only in git, real values never
cp deploy/env/api.env.example  deploy/env/api.env      # DATABASE_URL, …
cp deploy/env/n8n.env.example  deploy/env/n8n.env      # URLs, key, chat id, …
cp deploy/env/form.env.example deploy/env/form.env     # DEMO_N8N_WEBHOOK_URL
#    edit the three files (placeholders are CHANGE_ME)

# 5. Workflow — offline import BEFORE the first start (upsert by id)
deploy/bin/import-workflow.sh

# 6. Services
sudo cp deploy/systemd/lead-api.service deploy/systemd/lead-n8n.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lead-api lead-n8n

# 7. Reverse proxy (or install Caddy as a system service with this file)
export LEAD_DOMAIN=lead.example.com
export LEAD_WWW_ROOT=/opt/lead-intake/var/www
deploy/bin/render-form.sh
caddy validate --config deploy/caddy/Caddyfile
caddy run --config deploy/caddy/Caddyfile     # automatic HTTPS via Let's Encrypt

# 8. Verify (see sections below)
deploy/bin/healthcheck.sh
SMOKE_WEBHOOK_URL=https://lead.example.com/webhook/lead-intake deploy/bin/smoke.sh

# 9. Telegram, once: open the editor through an SSH tunnel, create the
#    "Telegram Lead Alerts" credential (bot token from @BotFather), point
#    both Telegram nodes at it; put the destination chat id in
#    TELEGRAM_CHAT_ID in n8n.env. The token lives only in n8n's encrypted
#    credential store (N8N_ENCRYPTION_KEY), never in a file in this repo.
```

Without systemd (any machine, no root): the identical path is
`deploy/bin/stack.sh start|stop|restart|status|logs`, which loads the same
env files, renders the form and runs the healthcheck before declaring
success. That is the path used for the validation below.

## Environment files and secrets

| File | Loaded by | Contains |
|---|---|---|
| `deploy/env/api.env` | systemd `EnvironmentFile=`, `stack.sh`, `run-api.sh` consumers | `DATABASE_URL`, `API_HOST/API_PORT`, optional `OPENAI_*` |
| `deploy/env/n8n.env` | systemd `EnvironmentFile=`, `stack.sh`, `import-workflow.sh` (fallback) | bind address/port, `N8N_WEBHOOK_URL`, `N8N_USER_FOLDER`, `N8N_ENCRYPTION_KEY`, `LEAD_API_BASE_URL`, `LEAD_QUALIFIED_THRESHOLD`, `N8N_BLOCK_ENV_ACCESS_IN_NODE`, `TELEGRAM_CHAT_ID` |
| `deploy/env/form.env` | `render-form.sh` (via `stack.sh`) | `DEMO_N8N_WEBHOOK_URL` |

Rules (enforced by `.gitignore`: `*.env`, `.env`, `.env.*`, `var/`, with the
`!.env.example` exception):

- Only `*.env.example` placeholders (`CHANGE_ME`) are committed.
- The Telegram **bot token exists nowhere in the repository** — only in n8n's
  encrypted credential store under `$N8N_USER_FOLDER/.n8n`.
- `N8N_ENCRYPTION_KEY` is generated once (`openssl rand -hex 32`), kept
  off-repo with the other secrets, and never rotated casually — changing it
  orphans every stored credential.
- `TEST_DATABASE_URL` is a test-only concern; it must not be set in
  production (tests keep using their separate database either way).
- A secret scan over the tracked files (rehearsal values: chat id,
  encryption key, DB DSN password, bot-token pattern) was clean — re-run it
  before every commit: `git grep -E '<your chat id>|<token pattern>'`.

## Running the stack

| Operation | No-root (validation path) | systemd (server path) |
|---|---|---|
| Start | `deploy/bin/stack.sh start` | `systemctl start lead-api lead-n8n` |
| Stop | `deploy/bin/stack.sh stop` | `systemctl stop lead-api lead-n8n` |
| Restart | `deploy/bin/stack.sh restart` | `systemctl restart lead-api lead-n8n` |
| Status | `deploy/bin/stack.sh status` | `systemctl status …` + `deploy/bin/healthcheck.sh` |
| Logs | `deploy/bin/stack.sh logs` (→ `var/log/{api,n8n}.log`) | `journalctl -u lead-api -u lead-n8n -f` |
| Migrations | `deploy/bin/migrate.sh` (or just start the API) | same |
| Workflow update | `deploy/bin/import-workflow.sh` then `stack.sh restart` | stop → `import-workflow.sh` → start |
| Tests | `.venv/bin/python -m pytest -q` → **83 passed** | — |

`stack.sh start` is deliberately strict: missing env file → die with the
copy-template hint; API `/health` or n8n `/healthz` not answering in time →
die with the log tail; workflow publication not converged → die (next
section); final `healthcheck.sh` failure → die. "started" means usable.

## Health & smoke verification

`deploy/bin/healthcheck.sh` distinguishes *process alive* from *usable*:

| Probe | Usable | Meaning of the alternatives |
|---|---|---|
| `GET /health` (FastAPI) | `200` | `503` = app up but PostgreSQL unreachable; no response = process down |
| `GET /healthz` (n8n) | `200` | the only health endpoint n8n 2.41.7 has; no response = process down |
| `POST {}` → production webhook | `422 validation_failed` | one shot proving webhook registered + workflow published + FastAPI reachable; `404` = registration lag (retried up to ~18 s, see below); `502` = n8n fine but FastAPI unreachable |

Exit code `0` only when everything is usable — suitable for a systemd
`ExecStartPost` or a cron alert. Tunables: `HEALTH_WEBHOOK_RETRIES` (6),
`HEALTH_WEBHOOK_INTERVAL` (3s).

`deploy/bin/smoke.sh` then drives the real path (webhook → API → PostgreSQL →
qualification → n8n execution data → Telegram) and asserts: not-qualified
`201` + row + `Log Normal Route`, qualified `201` + row + `Log Priority Route`
+ Telegram `ok:true`, duplicate `409` with no second row, invalid `422` with
no row, and cleanup of its own rows. It sends **one real Telegram message**
per run (the qualified case); use `SMOKE_WEBHOOK_URL` to aim it at the proxy.

## CORS model

| Topology | Origin relation | Mechanism |
|---|---|---|
| Production (this design) | same-origin: form and `/webhook/*` share the Caddy domain | no `Origin` mismatch, **no preflight, CORS never exercised**; Caddy adds no `Access-Control-*` headers |
| Local development | cross-origin: `http://localhost:8001` → `http://localhost:5679` | the workflow's Lead Webhook carries `parameters.options.allowedOrigins = "http://localhost:8001"` (the only M12 change to `n8n/lead-intake.json`) |

Verified against n8n 2.41.7 (browser semantics are server-independent here):

- allowed origin → `OPTIONS 204` + `Access-Control-Allow-Origin:
  http://localhost:8001`; webhook response carries the same ACAO;
- a non-listed origin gets a **mismatched** ACAO → the browser blocks the
  read (the n8n option is an allowlist, not a reflector);
- requests without an `Origin` header (curl, server-to-server) are processed
  normally — CORS is a browser mechanism, not authentication;
- production same-origin browser runs never emitted an `OPTIONS` request at
  all (asserted in the browser drill).

If the form is ever hosted on a *different* domain than the webhook, add that
origin to `allowedOrigins` (or serve the form from Caddy instead — the
recommended shape).

## Failure semantics (verified drills)

Each drill ran against the full rehearsal stack with restores afterwards;
evidence comes from HTTP codes, PostgreSQL rows and n8n's persisted execution
data (`deploy/bin/n8n_exec.py`).

| Drill | Expected guarantee | Observed |
|---|---|---|
| FastAPI stopped, valid submit through the proxy | `502 {"outcome":"upstream_error"}`, **no row**, **no Telegram**, failure visible in n8n | `502` + `upstream_error`; `Log Normal…`/qualified nodes never ran — execution reached `Respond Upstream Error` (`status=success`, the error branch is a designed path); no message sent; row count unchanged after API restart; healthcheck recovered |
| PostgreSQL stopped | `/health` → `503` (app up, DB down), healthcheck exits non-zero and says so; recovers without restart of the app | `503 {"detail":"Service unavailable"}`, `HEALTH FAILED` naming `503`; after `pg_ctl start` → `/health 200` and full healthcheck green (pool reconnects; no app crash) |
| Telegram destination faulted (invalid chat id in the *applied* workflow version) | intake stays `201` + row; failure visible as n8n execution error | `201 created` + row persisted; execution reached terminal `status=error` on the Telegram node; audit/notification build nodes had already run (record-before-send ordering from M10); no real message delivered; clean workflow restored and verified byte-for-byte against the committed JSON (current **and** applied version, outbox drained) |
| Scoring unavailable (bogus `OPENAI_API_KEY` + dead `OPENAI_BASE_URL`) | intake `201`, `score = NULL` (never `0`), manual-review branch + manual Telegram | `201` + `outcome=created`; row `score=NULL`; execution took `Log Manual Review` and delivered the manual-review message (`message_id` present); heuristic mode restored |
| Duplicate / invalid (smoke cases) | `409` no second row; `422` no row | both asserted, cleanup removed the run's rows |

Caller-isolation invariants (M9/M10) hold on this stack: the caller's answer
only ever reflects what FastAPI returned; operational or notification failures
surface as n8n execution errors, never as a changed response code.

## n8n operational notes

**Webhook registration lag.** n8n answers `/healthz` before it finishes
registering webhooks of the active workflow — a cold start can briefly serve
`404` on `/webhook/lead-intake`. This is expected for a few seconds;
`healthcheck.sh` retries the 404 case for ~18 s. Persistent 404 after that
means: workflow not imported/published (run `import-workflow.sh`) or n8n not
restarted after import. (Webhooks are deregistered on graceful shutdown —
another reason registration always re-runs at start.)

**Publication convergence (import → publish → restart → wait).** n8n 2.41.7
applies workflow publications **asynchronously**: a `publish:workflow` run
enqueues a publication that the running instance applies a few seconds later
(`workflow_publication_outbox`), and a freshly started instance first serves
its previously applied version before reconciling to
`workflow_entity.activeVersionId`. Until convergence, executions record the
**old** version — observed live while building this milestone (an execution
ran stale content seconds after an import). Therefore:

1. Always import **offline** (`import-workflow.sh` with n8n stopped), then
   start — the CLI itself prints *"Changes will not take effect if n8n is
   running"*.
2. `stack.sh start` waits (up to 120 s) until the applied pointer equals the
   desired active version **and** the outbox is drained, and fails loudly
   otherwise — so "stack up" implies "the imported workflow is what runs".
3. The same applies to systemd starts: the unit starts n8n after
   `import-workflow.sh` was run offline; if you must publish while running,
   restart afterwards and give it the convergence window.

**Persistence.** Everything n8n-owned lives in `$N8N_USER_FOLDER/.n8n`
(SQLite): workflow + version history, encrypted credentials, execution
history, webhook registration. It is under `var/` (rehearsal) or
`/var/lib/lead-intake` (production template) and gitignored — **never commit
it**. The FastAPI side persists in PostgreSQL; the form in `var/www`
(regenerable at any time by `render-form.sh`).

**Editor & credentials.** The editor is loopback-only; reach it through an
SSH tunnel. First-run setup (owner account) and creating the Telegram
credential happen there once. The rehearsal instead imported the already
encrypted credential with a pinned `N8N_ENCRYPTION_KEY`, proving the
CLI/provisioning path (`n8n import:credentials`); the UI path itself was not
exercised in this environment (see ledger).

**Version pinning.** The workflow is validated against n8n **2.41.7**;
`run-n8n.sh` can enforce it with `LEAD_N8N_EXPECT_VERSION=2.41.7`.

## Known warnings

Cosmetic, documented on purpose:

- `Running n8n outside a container is deprecated. Future versions will
  require running n8n via the official Docker image.` — printed on every
  start of n8n 2.41.7 outside Docker. This deployment intentionally runs
  bare (no container runtime on the validation host; containers out of
  scope). If n8n ever enforces this, the unit/run scripts are the
  integration point — the workflow JSON and env files are runtime-agnostic.
- Occasional `Error fetching from Strapi API … timeout` + a license-SDK
  line in `n8n.log` are n8n phoning home on startup; harmless, and
  `N8N_DIAGNOSTICS_ENABLED=false` (template default) turns off the
  telemetry that matters.

## Form configuration

The browser reads `config.js` (`window.LEAD_FORM_CONFIG.webhookUrl`) loaded
just above the form's script:

- **Local dev default:** the committed `demo/config.js`
  (`http://localhost:5679/webhook/lead-intake`) — never used in production.
- **Deployment:** `deploy/bin/render-form.sh` (run by `stack.sh start`)
  generates `var/www/config.js` from `form.env`'s `DEMO_N8N_WEBHOOK_URL`
  (`/webhook/lead-intake` for the same-origin shape) and copies the form to
  `var/www/{index.html,lead-form.html}`. The rendered root is never
  hand-edited; re-run the script after any change to `demo/` or `form.env`.
- **Fail loudly:** if `config.js` is missing or has no URL, the form shows a
  visible setup error and refuses to submit (verified: **0 POSTs** left the
  browser, console said exactly what to fix).

## Backup & restore

```bash
# Backup (stop n8n first for a consistent SQLite snapshot)
systemctl stop lead-n8n        # or stack.sh stop
sudo -u postgres pg_dump -Fc lead_automation > lead-automation.dump
tar czf n8n-data.tgz -C /var/lib/lead-intake .n8n
# also keep: deploy/env/*.env (secrets!), N8N_ENCRYPTION_KEY, this repo

# Restore: dump → env files → .n8n folder → import-workflow.sh (re-assert
# the committed workflow) → start lead-api + lead-n8n → healthcheck → smoke.
```

PostgreSQL is the authority for lead data; n8n's folder is the authority for
workflow/credentials/executions. Losing `N8N_ENCRYPTION_KEY` orphans the
stored Telegram credential even if the folder is intact.

## Verification ledger

Rehearsal topology (local, production-shaped): fresh throwaway PostgreSQL 16
cluster (own data dir, loopback, dedicated `lead_app` role — also proving
**fresh-database startup migrates automatically**), FastAPI on `:8010`, n8n
on `:5678` (broker `5681`, fresh data folder — proving **first-run
provisioning**), Caddy 2.11.7 on `:8080` using `Caddyfile.local` (same
routing as production, no TLS), form served same-origin through the proxy.

| Verified on this machine | Evidence |
|---|---|
| Full stack start/stop/restart, deterministic, logs | `stack.sh` runs in both batteries; `bash -n` on every script |
| Fresh-database startup + idempotent re-run of `sql/` migrations | lifespan migration on empty DB; `migrate.sh` → "idempotent" |
| Fresh-n8n-folder provisioning (offline import + publish + start) | pristine folder → workflow active, webhook registered |
| Health semantics (`200`/`503`/down, `/healthz`, chain probe, 404-retry) | `healthcheck.sh` green, and failing with named `503` when PG stopped |
| End-to-end smoke through the reverse proxy | **11/11** assertions incl. real Telegram `ok:true` |
| Failure drills (API/PG/Telegram/scoring) | [table above](#failure-semantics-verified-drills) — 29/29 assertions |
| Same-origin browser submission through the proxy (headless Chrome) | `POST → 201`, **no `OPTIONS` preflight**, success panel, console `201 created` |
| Missing-config guard (headless Chrome) | visible error, **0 POSTs** |
| CORS allow/deny behavior of the workflow option (curl probes) | `204`+matching ACAO / mismatched ACAO / no-Origin processed |
| Workflow JSON unchanged except `allowedOrigins` | `git diff` = 3 insertions, 1 deletion |
| Restart persistence (workflow active + webhook after restart) | `workflow: [('lead-intake',1)]`, `webhook: [('lead-intake','POST')]` |
| Both Caddyfiles valid + formatted | `caddy validate` & `caddy fmt` (v2.11.7) clean |
| systemd units well-formed | `systemd-analyze verify` on path-adapted copies → exit 0 |
| Test suite | `pytest -q` → **83 passed** (no test changes) |
| Secret scan of tracked files | chat id, encryption key, DSN password, bot-token pattern: absent |
| Publication convergence behavior + fix | observed stale-version execution live; `stack.sh` wait implemented and exercised by both batteries |

Not verified here (configuration-reviewed only — do not claim otherwise):

- public HTTPS end to end: real DNS, ports 80/443, ACME issuance/renewal;
- the units under a real systemd (boot order, `Protect*` hardening, service
  user, `journalctl`) — only `systemd-analyze verify`;
- the production `Caddyfile` serving traffic (validated; the identical
  routing shape ran via `Caddyfile.local` over plain HTTP);
- containerized operation (deliberately not shipped);
- n8n editor first-run/owner setup and credential creation through the UI;
- anything about a remote host, network latency, or HTTPS-only browser
  behavior (browser drills ran on `http://localhost:8080`, headless Chrome
  only).

## Out of scope (unchanged by this milestone)

Kubernetes, containers, microservices, Redis/queues/workers, multi-tenancy,
accounts/billing, monitoring stacks beyond the health endpoints, CDN/WAF,
infrastructure-as-code for a specific cloud, and the dashboard/CRM features
listed in the README.
