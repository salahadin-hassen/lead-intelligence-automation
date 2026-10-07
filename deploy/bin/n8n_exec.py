#!/usr/bin/env python3
"""Inspect n8n execution data (operational tool, no secrets printed).

n8n 2.x persists executions in a reference-pool format: element 0 holds the
run structure and every value is pooled, with digit-strings acting as
references. This tool resolves pools so searches run against real content.

Usage:
  n8n_exec.py list                     list executions (id, status, mode, times)
  n8n_exec.py nodes ID                 node start order for one execution
  n8n_exec.py search PATTERN [PATTERN...] [--after ID] [--timeout SECONDS]
      Find the first NEWER execution (id > --after, default = current max)
      whose resolved data contains every PATTERN. Polls until --timeout
      (default 0 = single pass). Exit 0 when found, 1 otherwise.

Database location (first that applies):
  --db PATH, env N8N_EXEC_DB, or $N8N_USER_FOLDER/.n8n/database.sqlite
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time


def db_path(args: argparse.Namespace) -> str:
    candidates = [args.db, os.environ.get("N8N_EXEC_DB")]
    folder = os.environ.get("N8N_USER_FOLDER")
    if folder:
        candidates.append(os.path.join(folder, ".n8n", "database.sqlite"))
    for candidate in candidates:
        if candidate:
            return candidate
    sys.exit("n8n_exec: no database path (set --db, N8N_EXEC_DB, or N8N_USER_FOLDER)")


def resolve(value, pool, depth=0):
    if depth > 60:
        return "<depth-limit>"
    if isinstance(value, str) and value.isdigit() and int(value) < len(pool):
        target = pool[int(value)]
        if isinstance(target, (dict, list)):
            return resolve(target, pool, depth + 1)
        return target
    if isinstance(value, dict):
        return {k: resolve(v, pool, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, pool, depth + 1) for v in value]
    return value


def load(con: sqlite3.Connection, exec_id: int):
    ent = con.execute(
        "select status, mode, startedAt, stoppedAt from execution_entity where id=?",
        (exec_id,),
    ).fetchone()
    blob = con.execute(
        "select data from execution_data where executionId=?", (exec_id,)
    ).fetchone()
    if blob is None:
        return ent, None
    pool = json.loads(blob[0])
    return ent, resolve(pool[0], pool)


def run_data(root):
    if not isinstance(root, dict):
        return {}
    result = root.get("resultData") or {}
    if not isinstance(result, dict):
        return {}
    runs = result.get("runData") or {}
    return runs if isinstance(runs, dict) else {}


def cmd_list(con, _args):
    for row in con.execute(
        "select id, status, mode, startedAt, stoppedAt "
        "from execution_entity order by id desc limit 30"
    ):
        print(f"exec {row[0]}: status={row[1]} mode={row[2]} start={row[3]} stop={row[4]}")


def cmd_nodes(con, args):
    ent, root = load(con, args.id)
    if ent is None:
        sys.exit(f"n8n_exec: execution {args.id} not found")
    print(f"execution {args.id}: status={ent[0]} mode={ent[1]} started={ent[2]}")
    flat = []
    for node, entries in run_data(root).items():
        if isinstance(entries, str):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                flat.append((entry.get("startTime", 0), node))
    for start, node in sorted(flat):
        print(f"  {node}  (start={start})")


def matches(con, exec_id, patterns) -> bool:
    _ent, root = load(con, exec_id)
    if root is None:
        return False
    blob = json.dumps(root, default=str)
    return all(p in blob for p in patterns)


def cmd_search(con, args):
    deadline = time.time() + max(args.timeout, 0)
    after = args.after
    if after is None:
        row = con.execute("select coalesce(max(id), 0) from execution_entity").fetchone()
        after = row[0]
    while True:
        ids = [
            r[0]
            for r in con.execute(
                "select id from execution_entity where id > ? order by id desc",
                (after,),
            )
        ]
        for exec_id in ids:
            if matches(con, exec_id, args.patterns):
                print(f"exec {exec_id} matched: " + " | ".join(args.patterns))
                return 0
        if time.time() >= deadline:
            print(
                f"n8n_exec: no execution newer than {after} matched all patterns "
                f"within {args.timeout}s"
            )
            return 1
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="path to n8n database.sqlite")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_nodes = sub.add_parser("nodes")
    p_nodes.add_argument("id", type=int)
    p_search = sub.add_parser("search")
    p_search.add_argument("patterns", nargs="+")
    p_search.add_argument("--after", type=int, default=None)
    p_search.add_argument("--timeout", type=float, default=0)
    args = parser.parse_args()

    con = sqlite3.connect(f"file:{db_path(args)}?mode=ro", uri=True, timeout=5)
    try:
        if args.cmd == "list":
            cmd_list(con, args)
        elif args.cmd == "nodes":
            cmd_nodes(con, args)
        else:
            return cmd_search(con, args)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
