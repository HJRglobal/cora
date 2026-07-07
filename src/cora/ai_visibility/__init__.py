"""Cora AI-Visibility Engine (Phase 1: measurement).

In-house "BeFound": ask buyer-style questions of grounded AI search engines,
detect whether F3 Energy / Pure / Mood are recommended, score it 0-100 per
brand, and surface it in Slack + on demand.

PHI guard is OFF for this feature -- it handles F3 marketing/visibility data
only, no health data (same posture as the influencer feature).

Sub-modules:
  prompts     -- frozen prompt-basket YAML loader
  classifier  -- Haiku LLM-as-judge (mentioned / correct-brand / position / sentiment)
  citations   -- URL resolve/HEAD-check + source-type tagging
  store       -- SQLite (scans / answers / mentions / citations / scores)
  scorer      -- 0-100 composite (presence 40 / SoV 25 / position 20 / sentiment 15)
  report      -- Slack score-card + on-demand tool text (reads the store)

Part B (gap->content optimization loop) is intentionally out of scope; the
`citations` table with source-type tagging is its seed. Do not build it here.
"""
