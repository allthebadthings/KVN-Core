# Rook Ops

A lightweight shared operations dashboard for Rook and worker agents.

## Purpose

Rook uses this dashboard as the shared operational state for tasks, blockers, retries, cost awareness, approvals, and next actions. The dashboard is deliberately small so Rook can restructure it later without having to invent the system from scratch.

## Included

- `index.html` — dashboard shell
- `styles.css` — dashboard styling
- `app.js` — local task board behavior and sample-state loader
- `sample-state.json` — starter operational state
- `task.schema.json` — task/state contract
- `ROOK_RULES.md` — Rook operating rules

## Current prototype behavior

The dashboard runs as a static page. It reads the starter state and stores local edits in browser localStorage. This is intentionally a prototype. The next production step is to replace localStorage with a small shared database/API while keeping the same task schema.

## Core rule

Rook owns outcomes, not just assignments. Repeated retries without a new hypothesis are prohibited.
