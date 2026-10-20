# Rook Ops

Shared operational state for Rook, worker agents, and Kevin. The browser dashboard, JSON API, and CLI all use one SQLite database; browser storage is not used.

## Run

Requires Python 3.10+ and no third-party packages.

```bash
cd rook-ops
cp .env.example .env                 # optional; export values using your service manager
python3 rook_ops.py serve
```

Open `http://127.0.0.1:8787`. Data is stored at `data/rook-ops.sqlite3` by default and is excluded from git. SQLite WAL mode and transactional writes allow the dashboard and agents to update the same live state safely.

Safe defaults bind only to loopback. If exposing the service on another interface, set a long `ROOK_OPS_TOKEN`, protect the endpoint with TLS/reverse proxy, and pass `Authorization: Bearer …`. `/api/health` intentionally remains unauthenticated.

## Agent CLI

The CLI speaks to the running API, so every agent observes the same state:

```bash
python3 rook_ops.py list
python3 rook_ops.py show ROOK-001
python3 rook_ops.py create --json '{"title":"Ship result","owner":"Rook","priority":"high","status":"in_progress","next_action":"Delegate the verified implementation"}'
python3 rook_ops.py update ROOK-001 --json '{"status":"blocked","blocker":"Upstream API unavailable","next_action":"Use the offline path"}'
python3 rook_ops.py attempt ROOK-001 --json '{"outcome":"failed","strategy":"remote API","error":"HTTP 503","evidence":"request id abc","cost":"0.12","credits_used":"1.5"}'
python3 rook_ops.py events --task-id ROOK-001
```

Set `ROOK_OPS_URL`, `ROOK_OPS_TOKEN`, and a distinct `ROOK_OPS_ACTOR` for each agent. JSON API routes are `GET/POST /api/tasks`, `GET/PATCH /api/tasks/:id`, `POST /api/tasks/:id/attempts`, and `GET /api/events`.

## Enforced workflow

1. Rook creates or updates the task with objective, owner, definition of done, strategy, and next action.
2. Every worker reads the current task before acting and records meaningful progress or an attempt afterward.
3. Failed attempts require strategy plus error/evidence. At three consecutive failures the circuit opens and the task becomes blocked.
4. Work can resume only after changing strategy, hypothesis, or owner. Blind retries are rejected by the API.
5. Approval tasks require the exact approval detail. Blocked tasks require a blocker and recommended next action.
6. A task cannot become `done` without verification evidence. Rook remains responsible for the verified outcome.

Costs and credits are stored as integer millionths to avoid floating-point loss. Updates include optimistic task versions; stale dashboard writes receive a conflict and must reload.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile rook_ops.py
```
