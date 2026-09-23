"""Deposco order-push write path (Phase 2/3): spec -> payload -> preflight ->
pending -> card -> tap -> push -> read-back.

Design of record: `02-F3-Energy/projects/2026-08_deposco-api-order-automation/
_notes/2026-09-08_f3e_DESIGN-PROPOSAL-deposco-phase2-3-push.md`. NO LLM
anywhere in this package (Fork 1, re-ruled 2026-09-09 item 4): every module
here is deterministic, and spec authoring is human-only by design -- there is
no code path in this package that accepts free text and turns it into a spec.
"""
