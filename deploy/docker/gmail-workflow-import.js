#!/usr/bin/env node
/**
 * Gmail-only workflow import helper for deploy/docker/n8n-entrypoint.sh.
 *
 *   prepare <repo.json> <out.json> [existing-export.json]
 *     Writes the workflow to import and exits 0, or exits without writing:
 *       3  already applied: the stored workflow came from this exact repo file
 *       4  refused: the stored workflow is active (an import would deactivate it)
 *     The repo file's sha256 is stamped into meta.leadintelSourceSha256, so a
 *     force flag left set does not re-import the same file on every restart.
 *     Credential references (per node, same credential type) and the Gmail
 *     Trigger's label selection are copied from the stored workflow, because
 *     n8n's import replaces nodes wholesale and would reset them to the
 *     repository placeholders.
 *
 *   verify <repo.json> <post-import-export.json>
 *     Exits 0 only if the stored workflow is inactive and carries this file's hash.
 *
 * Only reads and writes local files; all database access stays in the n8n CLI.
 */
"use strict";

const crypto = require("crypto");
const fs = require("fs");

const WORKFLOW_ID = "gmail-lead-intake";
const EXIT_ALREADY_APPLIED = 3;
const EXIT_REFUSED_ACTIVE = 4;

function fail(message) {
  console.error(`[gmail-workflow-import] ERROR: ${message}`);
  process.exit(1);
}

function sourceHash(repoPath) {
  return crypto.createHash("sha256").update(fs.readFileSync(repoPath)).digest("hex");
}

// n8n export:workflow writes an array; import accepts either shape.
function readStoredWorkflow(exportPath) {
  const parsed = JSON.parse(fs.readFileSync(exportPath, "utf8"));
  const list = Array.isArray(parsed) ? parsed : [parsed];
  const workflow = list.find((w) => w && w.id === WORKFLOW_ID);
  if (!workflow) fail(`${exportPath} does not contain workflow ${WORKFLOW_ID}`);
  return workflow;
}

function isActive(workflow) {
  return workflow.active === true || Boolean(workflow.activeVersionId);
}

function prepare(repoPath, outPath, existingPath) {
  const workflow = JSON.parse(fs.readFileSync(repoPath, "utf8"));
  if (workflow.id !== WORKFLOW_ID) fail(`${repoPath} is not the ${WORKFLOW_ID} workflow`);
  const hash = sourceHash(repoPath);

  if (existingPath) {
    const stored = readStoredWorkflow(existingPath);
    if (isActive(stored)) {
      console.error(`[gmail-workflow-import] REFUSED: ${WORKFLOW_ID} is active; deactivate it in the n8n UI first.`);
      process.exit(EXIT_REFUSED_ACTIVE);
    }
    if ((stored.meta || {}).leadintelSourceSha256 === hash) {
      process.exit(EXIT_ALREADY_APPLIED);
    }
    const storedNodes = new Map((stored.nodes || []).map((n) => [n.name, n]));
    for (const node of workflow.nodes) {
      const previous = storedNodes.get(node.name);
      if (!previous) continue;
      for (const [type, ref] of Object.entries(previous.credentials || {})) {
        if (node.credentials && type in node.credentials) node.credentials[type] = ref;
      }
      const labelIds = previous.parameters && previous.parameters.filters && previous.parameters.filters.labelIds;
      if (node.type === "n8n-nodes-base.gmailTrigger" && Array.isArray(labelIds) && labelIds.length) {
        node.parameters.filters.labelIds = labelIds;
      }
    }
  }

  workflow.active = false;
  workflow.meta = { ...(workflow.meta || {}), leadintelSourceSha256: hash };
  fs.writeFileSync(outPath, JSON.stringify(workflow, null, 2));
}

function verify(repoPath, exportPath) {
  const stored = readStoredWorkflow(exportPath);
  if (isActive(stored)) fail(`${WORKFLOW_ID} is active after import`);
  if ((stored.meta || {}).leadintelSourceSha256 !== sourceHash(repoPath)) {
    fail(`${WORKFLOW_ID} in the database does not match ${repoPath}`);
  }
}

const [command, ...args] = process.argv.slice(2);
try {
  if (command === "prepare" && args.length >= 2) prepare(args[0], args[1], args[2]);
  else if (command === "verify" && args.length === 2) verify(args[0], args[1]);
  else fail("usage: gmail-workflow-import.js prepare <repo.json> <out.json> [existing.json] | verify <repo.json> <export.json>");
} catch (err) {
  fail(err.message);
}
