# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Feishu (Lark) bot that receives messages via webhook and dispatches them to a local Claude Code CLI instance, then returns results back to Feishu. Runs on WSL2 with ngrok for public webhook exposure.

## Running

```bash
pip install -r requirements.txt
python bot.py          # Starts FastAPI server on port 8000
ngrok http 8000        # Separate terminal — tunnel for Feishu webhook delivery
```

## Architecture

**Single-process design** — `bot.py` is the entire application:

- **FastAPI server** (`/webhook`, `/health`) — receives Feishu event pushes, handles URL verification handshake
- **Event processing** (`on_message_receive` → `handle_user_message` / `handle_image_message`) — parses message, dispatches to Claude Code in a daemon thread
- **Claude Code invocation** (`_run_claude`) — spawns `claude` CLI as subprocess with `--output-format stream-json`, parses JSON lines for session_id, tool uses, and results. Sends progress updates to Feishu every 30s during execution
- **Session management** (`SessionStore`) — thread-safe file-backed store (`sessions.json`) binding `(open_id, cwd)` → `session_id`, with TTL expiry and message count limits. Per-user locks prevent concurrent resume of the same session
- **Message sending** (`send_message`) — auto-splits messages exceeding 4000 chars

**Config** (`config.py`) — all settings via env vars with hardcoded defaults. Includes `PROJECT_MAP` for `@project_name` message prefix switching.

## Key Behaviors

- **Auto mode** (default): runs Claude with `--dangerously-skip-permissions`
- **Safe mode**: triggered by `/safe` message prefix — Claude runs without skip-permissions flag, denied operations are reported back
- **Project switching**: `@feishu fix bot.py` sets CWD to the mapped directory before invoking Claude
- **Session resume**: reuses existing Claude session per `(user, project)`, falls back to new session if resume fails
- **Image handling**: downloads Feishu images to `/tmp/feishu_images/{image_key}.jpg` and returns the local path

## User Commands (in Feishu chat)

- `/new` — clear all sessions; `/new @project` — clear specific project session
- `/session` — show active sessions info
- `/safe <prompt>` — run in safe mode (no auto-permission-skip)
- `@<project> <prompt>` — switch project directory

## Dependencies

- `lark-oapi` — Feishu/Lark SDK (event decryption, signature verification, message API)
- `fastapi` + `uvicorn` — HTTP server
- Local `claude` CLI must be installed and available in PATH
