"""F3E blog content lane: draft -> deterministic preflight -> stage unpublished
-> Harrison one-tap publish.

Harrison ruled 2026-08-26: auto-draft, one-tap approve, publish. Full-auto
publishing was REJECTED and Harrison is the sole publisher.

The load-bearing split inside this package:

    LLM            -> drafting ONLY (f3e_blog.drafting)
    deterministic  -> preflight, staging, publish, read-back, ledger
                      (f3e_blog.preflight / .pipeline / .publish_cards)

No model output is ever trusted to decide whether copy is publishable, and no
model can reach the publish path at all: `shopify_client.publish_article` is
called from exactly one place, the human confirm-tap handler.
"""
