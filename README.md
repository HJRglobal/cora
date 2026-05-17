# Cora

Entity-aware Slack Q&A bot for the HJR workspace. @-mention `@Cora` in any channel she's invited to and get a grounded, entity-specific answer drawn from the Founder OS memory layer.

**Full project brief:** `G:\My Drive\HJR-Founder-OS\_shared\projects\cora\CLAUDE.md`
**Phase 1 plan:** `G:\My Drive\HJR-Founder-OS\_shared\projects\cora\design\phase-1-plan.md`

---

## Quick start (dev)

**Prerequisites:** Python 3.12+, [uv](https://docs.astral.sh/uv/), a Cloudflare account (free tier), Git for Windows.

```powershell
# 1. Clone and enter the repo
git clone <repo-url>
cd cora

# 2. Install dependencies
uv sync

# 3. Copy and fill in secrets
cp .env.example .env
# Edit .env with your Slack bot token, signing secret, and Anthropic API key

# 4. Register the pre-commit hook (one-time, per checkout)
git config core.hooksPath .githooks

# 5. Run the bot (Socket Mode — no tunnel needed for dev)
uv run python -m cora.main
```

---

## Project structure

```
src/cora/           Python package — bot logic
design/             System prompts + channel routing config
slack-app-config/   Slack app manifest
deployment/         Runbook + Task Scheduler config
logs/               Runtime logs (gitignored)
.githooks/          pre-commit secret scanner
```

---

## Channel routing

Channel → entity mapping lives in `design/channel-routing.yaml`. Edit that file to add channels; no code change needed.

---

## Secrets

All secrets live in `.env` (gitignored). See `.env.example` for required keys. A pre-commit hook blocks commits that contain `sk-ant-`, `xoxb-`, or `xoxp-` prefixes — this is non-negotiable per architecture decision #10.

---

## Deployment

See `deployment/runbook.md` for restart procedures, log rotation, token rotation, and Windows Task Scheduler setup.
