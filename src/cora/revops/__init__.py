"""Revenue-ops loop: cadence ledger + send-trust ladder + silence-nudge playbook.

Design of record: 00-Founder/projects/review-cora-capabilities-and-roadmap/
2026-08-01_fndr_revenue-ops-loop-design.md (locked by Harrison 2026-08-01).

Ladder invariants (enforced in code, never prompts):
- Tier 0 draft-only is the default for everything.
- Tier 1 (approve-then-send) exists for exactly ONE playbook in v1: silence_nudge.
- Tier 2 is config-schema-only; the loader hard-rejects any tier-2 entry.
- CORA_SEND_LIVE=off (default) beats everything, including an approved card.
- Every Tier-1 send is a reply on an existing thread; recipients must be a
  subset of live thread participants; mailbox allowlist v1 = harrison@hjrglobal.com.
- LEX never enters the ledger, the sweep, or any send path (excluded at ingest).
"""
