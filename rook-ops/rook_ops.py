#!/usr/bin/env python3
"""Rook Ops shared SQLite store, JSON API, static server, and agent CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "rook-ops.sqlite3"
STATUSES = {"todo", "in_progress", "blocked", "approval", "done"}
PRIORITIES = {"high", "medium", "low"}
CIRCUIT_BREAKER = 3
SCHEMA_VERSION = 1


class ValidationError(Exception):
    pass


class ConflictError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def money_to_micros(value) -> int:
    if value in (None, ""):
        return 0
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError("cost must be a decimal amount") from exc
    if amount < 0:
        raise ValidationError("cost cannot be negative")
    return int((amount * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rowdict(row):
    return dict(row) if row else None


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def migrate(self):
        with self._lock, closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
                INSERT INTO schema_meta(version)
                  SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);
                CREATE TABLE IF NOT EXISTS tasks(
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT NOT NULL DEFAULT '',
                  owner TEXT NOT NULL, priority TEXT NOT NULL, status TEXT NOT NULL,
                  blocker TEXT NOT NULL DEFAULT '', hypothesis TEXT NOT NULL DEFAULT '',
                  strategy TEXT NOT NULL DEFAULT '', next_action TEXT NOT NULL,
                  approval_required INTEGER NOT NULL DEFAULT 0,
                  approval_detail TEXT NOT NULL DEFAULT '',
                  definition_of_done TEXT NOT NULL DEFAULT '',
                  verification TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '',
                  cost_micros INTEGER NOT NULL DEFAULT 0,
                  credits_used_micros INTEGER NOT NULL DEFAULT 0,
                  failed_attempts INTEGER NOT NULL DEFAULT 0,
                  circuit_open INTEGER NOT NULL DEFAULT 0,
                  version INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                  outcome TEXT NOT NULL, strategy TEXT NOT NULL, hypothesis TEXT NOT NULL DEFAULT '',
                  evidence TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                  cost_micros INTEGER NOT NULL DEFAULT 0,
                  credits_used_micros INTEGER NOT NULL DEFAULT 0,
                  actor TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                  type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id DESC);
            """)
            db.commit()

    def _event(self, db, task_id, kind, actor, payload):
        db.execute("INSERT INTO events(task_id,type,actor,payload,created_at) VALUES(?,?,?,?,?)",
                   (task_id, kind, actor, json.dumps(payload, separators=(",", ":")), now()))

    def _validate(self, data, existing=None):
        merged = {**(existing or {}), **data}
        for field in ("title", "owner", "next_action"):
            if not str(merged.get(field, "")).strip():
                raise ValidationError(f"{field} is required")
        if merged.get("status", "todo") not in STATUSES:
            raise ValidationError("invalid status")
        if merged.get("priority", "medium") not in PRIORITIES:
            raise ValidationError("invalid priority")
        approval = bool(merged.get("approval_required")) or merged.get("status") == "approval"
        if approval and not str(merged.get("approval_detail", "")).strip():
            raise ValidationError("approval_detail is required when approval is required")
        if merged.get("status") == "blocked" and not str(merged.get("blocker", "")).strip():
            raise ValidationError("blocker is required for blocked tasks")
        if merged.get("status") == "done" and not str(merged.get("verification", "")).strip():
            raise ValidationError("verification is required before a task can be done")
        return merged

    def _hydrate(self, db, row, include_attempts=True):
        item = rowdict(row)
        if not item:
            return None
        item["approval_required"] = bool(item["approval_required"])
        item["circuit_open"] = bool(item["circuit_open"])
        item["cost"] = f'{Decimal(item["cost_micros"]) / Decimal(1_000_000):.6f}'.rstrip("0").rstrip(".") or "0"
        item["credits_used"] = f'{Decimal(item["credits_used_micros"]) / Decimal(1_000_000):.6f}'.rstrip("0").rstrip(".") or "0"
        if include_attempts:
            item["attempts"] = [rowdict(r) for r in db.execute(
                "SELECT * FROM attempts WHERE task_id=? ORDER BY id DESC", (item["id"],))]
        return item

    def create_task(self, data, actor="Rook"):
        data = dict(data)
        data.setdefault("id", f"TASK-{uuid.uuid4().hex[:8].upper()}")
        data.setdefault("owner", "Rook")
        data.setdefault("priority", "medium")
        data.setdefault("status", "todo")
        data.setdefault("next_action", "Rook must choose and record the next action.")
        merged = self._validate(data)
        timestamp = now()
        fields = ["id", "title", "objective", "owner", "priority", "status", "blocker", "hypothesis",
                  "strategy", "next_action", "approval_required", "approval_detail", "definition_of_done",
                  "verification", "outcome"]
        values = [merged.get(f, "") for f in fields]
        values[fields.index("approval_required")] = int(bool(merged.get("approval_required") or merged["status"] == "approval"))
        with self._lock, closing(self.connect()) as db:
            try:
                db.execute(f"INSERT INTO tasks({','.join(fields)},cost_micros,credits_used_micros,created_at,updated_at) VALUES({','.join('?' for _ in fields)},?,?,?,?)",
                           (*values, money_to_micros(merged.get("cost")), money_to_micros(merged.get("credits_used")), timestamp, timestamp))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("task id already exists") from exc
            self._event(db, merged["id"], "task.created", actor, {"title": merged["title"]})
            db.commit()
            return self.get_task(merged["id"])

    def list_tasks(self, status=None):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM tasks" + (" WHERE status=?" if status else "") + " ORDER BY updated_at DESC",
                              (status,) if status else ()).fetchall()
            return [self._hydrate(db, r, False) for r in rows]

    def get_task(self, task_id):
        with closing(self.connect()) as db:
            item = self._hydrate(db, db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            if not item:
                raise KeyError(task_id)
            return item

    def update_task(self, task_id, data, actor="Rook"):
        with self._lock, closing(self.connect()) as db:
            old = self._hydrate(db, db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(), False)
            if not old:
                raise KeyError(task_id)
            if "version" in data and int(data["version"]) != old["version"]:
                raise ConflictError("task changed; reload before updating")
            merged = self._validate(data, old)
            if old["circuit_open"] and data.get("status") == "in_progress":
                changed = any(str(merged.get(k, "")).strip() != str(old.get(k, "")).strip()
                              for k in ("strategy", "hypothesis", "owner"))
                if not changed:
                    raise ConflictError("circuit is open; change strategy, hypothesis, or owner before retrying")
                merged["circuit_open"] = False
                merged["failed_attempts"] = 0
            editable = ["title", "objective", "owner", "priority", "status", "blocker", "hypothesis", "strategy",
                        "next_action", "approval_required", "approval_detail", "definition_of_done", "verification", "outcome"]
            assignments, values, changed = [], [], {}
            for field in editable:
                value = int(bool(merged.get(field))) if field == "approval_required" else merged.get(field, "")
                if value != old.get(field):
                    assignments.append(f"{field}=?"); values.append(value); changed[field] = value
            for source, target in (("cost", "cost_micros"), ("credits_used", "credits_used_micros")):
                if source in data:
                    value = money_to_micros(data[source])
                    assignments.append(f"{target}=?"); values.append(value); changed[target] = value
            if "circuit_open" in merged and int(bool(merged["circuit_open"])) != int(old["circuit_open"]):
                assignments += ["circuit_open=?", "failed_attempts=?"]
                values += [int(bool(merged["circuit_open"])), int(merged["failed_attempts"])]
            if assignments:
                assignments += ["version=version+1", "updated_at=?"]
                values += [now(), task_id]
                db.execute(f"UPDATE tasks SET {','.join(assignments)} WHERE id=?", values)
                self._event(db, task_id, "task.updated", actor, changed)
                db.commit()
        return self.get_task(task_id)

    def record_attempt(self, task_id, data, actor="Rook"):
        outcome = data.get("outcome")
        if outcome not in {"failed", "succeeded"}:
            raise ValidationError("attempt outcome must be failed or succeeded")
        strategy = str(data.get("strategy", "")).strip()
        if not strategy:
            raise ValidationError("attempt strategy is required")
        if outcome == "failed" and not (str(data.get("error", "")).strip() or str(data.get("evidence", "")).strip()):
            raise ValidationError("failed attempts require error or evidence")
        with self._lock, closing(self.connect()) as db:
            task = self._hydrate(db, db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone(), False)
            if not task:
                raise KeyError(task_id)
            if task["circuit_open"]:
                raise ConflictError("circuit is open; update task with a changed strategy, hypothesis, or owner first")
            cost = money_to_micros(data.get("cost")); credits = money_to_micros(data.get("credits_used"))
            db.execute("INSERT INTO attempts(task_id,outcome,strategy,hypothesis,evidence,error,cost_micros,credits_used_micros,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (task_id, outcome, strategy, data.get("hypothesis", ""), data.get("evidence", ""), data.get("error", ""), cost, credits, actor, now()))
            failures = task["failed_attempts"] + 1 if outcome == "failed" else 0
            circuit = failures >= CIRCUIT_BREAKER
            status = "blocked" if circuit else task["status"]
            blocker = ("Circuit breaker opened after repeated failures; Rook must change strategy."
                       if circuit else task["blocker"])
            next_action = ("Record a new hypothesis and change strategy, owner, split, or escalation before retrying."
                           if circuit else task["next_action"])
            db.execute("UPDATE tasks SET failed_attempts=?,circuit_open=?,status=?,blocker=?,next_action=?,cost_micros=cost_micros+?,credits_used_micros=credits_used_micros+?,version=version+1,updated_at=? WHERE id=?",
                       (failures, int(circuit), status, blocker, next_action, cost, credits, now(), task_id))
            self._event(db, task_id, "attempt.recorded", actor, {"outcome": outcome, "strategy": strategy, "circuit_open": circuit})
            db.commit()
        return self.get_task(task_id)

    def events(self, task_id=None, limit=100):
        limit = max(1, min(int(limit), 500))
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM events" + (" WHERE task_id=?" if task_id else "") + " ORDER BY id DESC LIMIT ?",
                              (task_id, limit) if task_id else (limit,)).fetchall()
            result = []
            for row in rows:
                item = rowdict(row); item["payload"] = json.loads(item["payload"]); result.append(item)
            return result


class ApiHandler(SimpleHTTPRequestHandler):
    server_version = "RookOps/1"

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    @property
    def store(self): return self.server.store

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.log_date_time_string()} {fmt % args}\n")

    def _json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)

    def _authorized(self):
        token = self.server.token
        return not token or self.headers.get("Authorization") == f"Bearer {token}"

    def _body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1_000_000: raise ValidationError("request too large")
        try: return json.loads(self.rfile.read(size) or b"{}")
        except json.JSONDecodeError as exc: raise ValidationError("invalid JSON") from exc

    def _route(self, method):
        parsed = urlparse(self.path); path = parsed.path.rstrip("/")
        if path == "/api/health" and method == "GET":
            return self._json(200, {"ok": True, "schema_version": SCHEMA_VERSION})
        if not path.startswith("/api/"): return False
        if not self._authorized(): return self._json(401, {"error": "unauthorized"})
        actor = self.headers.get("X-Rook-Actor", "api")[:120]
        if path == "/api/tasks" and method == "GET":
            return self._json(200, {"tasks": self.store.list_tasks(parse_qs(parsed.query).get("status", [None])[0])})
        if path == "/api/tasks" and method == "POST":
            return self._json(201, {"task": self.store.create_task(self._body(), actor)})
        if path == "/api/events" and method == "GET":
            qs = parse_qs(parsed.query); return self._json(200, {"events": self.store.events(qs.get("task_id", [None])[0], qs.get("limit", [100])[0])})
        match = re.fullmatch(r"/api/tasks/([^/]+)(/attempts)?", path)
        if match:
            task_id, attempts = match.groups()
            if attempts and method == "POST": return self._json(201, {"task": self.store.record_attempt(task_id, self._body(), actor)})
            if not attempts and method == "GET": return self._json(200, {"task": self.store.get_task(task_id)})
            if not attempts and method in {"PATCH", "PUT"}: return self._json(200, {"task": self.store.update_task(task_id, self._body(), actor)})
        self._json(404, {"error": "not found"}); return True

    def _handle(self, method):
        try:
            handled = self._route(method)
            if handled is False:
                if method == "GET": return super().do_GET()
                self._json(404, {"error": "not found"})
        except ValidationError as exc: self._json(422, {"error": str(exc)})
        except ConflictError as exc: self._json(409, {"error": str(exc)})
        except KeyError: self._json(404, {"error": "task not found"})
        except Exception as exc:
            self.log_error("unhandled API error: %s", exc); self._json(500, {"error": "internal error"})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PATCH(self): self._handle("PATCH")
    def do_PUT(self): self._handle("PUT")


def request(method, url, payload=None, token=None, actor="Rook"):
    from urllib.request import Request, urlopen
    headers = {"Content-Type": "application/json", "X-Rook-Actor": actor}
    if token: headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
    with urlopen(req) as response: return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rook Ops shared operations CLI")
    parser.add_argument("--db", default=os.getenv("ROOK_OPS_DB", str(DEFAULT_DB)))
    parser.add_argument("--url", default=os.getenv("ROOK_OPS_URL", "http://127.0.0.1:8787/api"))
    parser.add_argument("--token", default=os.getenv("ROOK_OPS_TOKEN", ""))
    parser.add_argument("--actor", default=os.getenv("ROOK_OPS_ACTOR", "Rook"))
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve"); serve.add_argument("--host", default=os.getenv("ROOK_OPS_HOST", "127.0.0.1")); serve.add_argument("--port", type=int, default=int(os.getenv("ROOK_OPS_PORT", "8787")))
    sub.add_parser("list")
    show = sub.add_parser("show"); show.add_argument("id")
    create = sub.add_parser("create"); create.add_argument("--json", required=True)
    update = sub.add_parser("update"); update.add_argument("id"); update.add_argument("--json", required=True)
    attempt = sub.add_parser("attempt"); attempt.add_argument("id"); attempt.add_argument("--json", required=True)
    events = sub.add_parser("events"); events.add_argument("--task-id")
    args = parser.parse_args(argv)
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.token:
            parser.error("ROOK_OPS_TOKEN is required when binding beyond loopback")
        httpd = ThreadingHTTPServer((args.host, args.port), ApiHandler); httpd.store = Store(args.db); httpd.token = args.token
        print(f"Rook Ops listening on http://{args.host}:{args.port}", flush=True)
        try: httpd.serve_forever()
        except KeyboardInterrupt: pass
        finally: httpd.server_close()
        return
    base = args.url.rstrip("/")
    if args.command == "list": result = request("GET", f"{base}/tasks", token=args.token, actor=args.actor)
    elif args.command == "show": result = request("GET", f"{base}/tasks/{args.id}", token=args.token, actor=args.actor)
    elif args.command == "create": result = request("POST", f"{base}/tasks", json.loads(args.json), args.token, args.actor)
    elif args.command == "update": result = request("PATCH", f"{base}/tasks/{args.id}", json.loads(args.json), args.token, args.actor)
    elif args.command == "attempt": result = request("POST", f"{base}/tasks/{args.id}/attempts", json.loads(args.json), args.token, args.actor)
    else: result = request("GET", f"{base}/events" + (f"?task_id={args.task_id}" if args.task_id else ""), token=args.token, actor=args.actor)
    print(json.dumps(result, indent=2))


if __name__ == "__main__": main()
