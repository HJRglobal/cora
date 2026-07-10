# Efficiency Backlog

_Harrison-approved findings from the weekly friction-mining pass (Org Synthesis Phase 3). Append-only; newest last._

## [2026-06-23] Create task status decision tree for W9 collection workflow

- Signal: repeated_question | Entity: FNDR | observed 6x in the last 14 days
- Route: doc
- Recommendation: Harrison is repeating the same three-part question (update needed? new due date? close task?) across the same task 6 times in 14 days, suggesting unclear acceptance criteria or status checkpoints. Document a simple decision tree or checklist for the W9 task lifecycle—clarifying when to update, reschedule, or close—and attach it to the task template or workflow. This eliminates ambiguity without requiring automation.
- Evidence: Does it need an update, a new due date, or can it be closed?; Does it need an update, a new due date, or can it be closed?

## [2026-06-23] Automate monthly Buzzsprout podcast invoice uploads

- Signal: repeated_manual_steps | Entity: HJRPROD | observed 8x in the last 14 days
- Route: make_com
- Recommendation: HJRPROD is manually uploading invoices to Buzzsprout 8 times in 14 days, consuming 2–3 hours per occurrence. This is a rule-based mechanical task (fetch invoice, upload to platform) that can be automated via Make.com to integrate Buzzsprout's API with your invoice system, eliminating repetitive manual steps.
- Evidence: *Upload 3 hours each month *; *Upload 3 hours each month *

## [2026-06-23] Standardize Frontier Airlines email template UTM parameters across campaigns

- Signal: repeated_question | Entity: FNDR | observed 8x in the last 14 days
- Route: make_com
- Recommendation: The evidence shows 8 instances in 14 days where Frontier email campaigns use inconsistent UTM content parameters (footer_logo, footer_1, footer_2, footer_3) pointing to the same or similar destination URLs. Implement a Make.com scenario to normalize these parameters to a single standard template value, reducing tracking fragmentation and simplifying analytics reporting across F3E and UFL email sends.
- Evidence: <https://flights.flyfrontier.com/en/?utm_source=iterable&utm_medium=email&utm_campaign=%%emailname%%&utm_content=footer_; <https://flights.flyfrontier.com/en/?utm_source=iterable&utm_medium=email&utm_campaign=%%emailname%%&utm_content=footer_

## [2026-06-23] Reduce email volume notifications for founder

- Signal: repeated_question | Entity: FNDR | observed 10x in the last 14 days
- Route: process_change
- Recommendation: Harrison is receiving repetitive "Getting too many emails?" notifications (10x in 14 days), suggesting either misconfigured alert settings or a notification system generating duplicate messages. Audit the notification rules triggering these alerts and either disable redundant ones or consolidate them into a single daily digest. This is a quick configuration fix that will reduce cognitive friction.
- Evidence: Getting too many emails?; Getting too many emails?
