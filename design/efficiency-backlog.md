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

## [2026-07-27] Consolidate Asana notification preferences across FNDR entities

- Signal: repeated_question | Entity: FNDR | observed 13x in the last 14 days
- Route: doc
- Recommendation: The founder and ops manager are receiving 13 notification-preference-change prompts in 14 days across multiple Asana notification categories (work updates, overdue tasks, upcoming tasks). This suggests either misconfigured default settings or incomplete initial setup. Create a shared known-answer doc or playbook documenting optimal Asana notification settings for the FNDR team, then apply those settings uniformly to prevent repeated friction.
- Evidence: Change what Asana sends you (https://app.asana.com/-/confirm_change_notification_category_setting?domain=682743441507584; Change what Asana sends you (https://app.asana.com/-/confirm_change_notification_category_setting?domain=682743441507584

## [2026-08-10] Automate Trial Reel completion status checks in Asana

- Signal: repeated_question | Entity: BDM | observed 12x in the last 14 days
- Route: make_com
- Recommendation: Cora is repeatedly asking the BDM Creative Director to manually check and mark trial reel tasks complete in Asana (12 times in 14 days). This is a rule-based mechanical task: check task status, mark complete if done. Implement a Make.com scenario that monitors these specific Asana tasks and auto-completes them based on a completion trigger (e.g., file uploaded, status changed, or timestamp), eliminating the manual Slack reminders.
- Evidence: - Trial Reels - At Least I Have The Balls To Try Everything [https://app.asana.com/1/682743441507584/project/12113059463; - Trial Reels - At Least I Have The Balls To Try Everything [https://app.asana.com/1/682743441507584/project/12113059463

## [2026-08-10] Reduce Qualtrics survey reminder frequency to Operations Manager

- Signal: repeated_question | Entity: FNDR | observed 12x in the last 14 days
- Route: known_answer
- Recommendation: Hannah Grant is receiving identical survey reminder emails 12 times in 14 days from Staples/Qualtrics. Implement a known-answer entry or brief doc that clarifies the survey's purpose, deadline, and submission process, then adjust the reminder cadence (e.g., max 2-3 reminders) in Qualtrics settings. This eliminates inbox friction while ensuring feedback is still collected.
- Evidence: <https://staples.qualtrics.com/jfe/form/SV_55wGLVksqRChGGa?Q_DL=yUBq8Iz9q9WYVNX_55wGLVksqRChGGa_CTR_fDKAJDgPWcelU1p&Q_RD; <https://staples.qualtrics.com/jfe/form/SV_55wGLVksqRChGGa?Q_DL=yUBq8Iz9q9WYVNX_55wGLVksqRChGGa_CTR_fDKAJDgPWcelU1p&Q_RD

## [2026-08-10] Automate Asana task completion status checks from Slack requests

- Signal: repeated_question | Entity: BDM | observed 11x in the last 14 days
- Route: make_com
- Recommendation: Cora is receiving 11 identical requests in 14 days to check and mark tasks complete in Asana. This is a repetitive, rule-based mechanical request with no context variation. Implement a Make.com scenario that monitors for this Slack message pattern and automatically queries Asana task status, then either marks tasks complete or surfaces them for manual review if status is ambiguous.
- Evidence: - Girls that do it all [https://app.asana.com/1/682743441507584/project/1211305946355255/task/1211332922442264]Could you; - Girls that do it all [https://app.asana.com/1/682743441507584/project/1211305946355255/task/1211332922442264]Could you

## [2026-08-10] Automate or consolidate repeated paperless enrollment messaging

- Signal: repeated_question | Entity: FNDR | observed 9x in the last 14 days
- Route: process_change
- Recommendation: The founder received 9 identical enrollment reminder emails in 14 days, indicating either a misconfigured automated campaign or manual redundancy. Clarify whether this is a vendor-side issue (Bristol West) requiring account audit, a Make.com automation loop that needs rule-based correction, or a process gap where enrollment instructions should be documented once as a known-answer resource. Route to Operations Manager (Hannah Grant) to diagnose root cause and implement single-source solution.
- Evidence: https://click.email.bristolwest.com/?qs=ABB7InYiOjEsImQiOjQ5NjJ9AAEAAAAAAWvQmxxPQSlQLqK1Y1ovyU-u9YHZi2AsWLYIQPfp8IJKcDsa; https://click.email.bristolwest.com/?qs=ABB7InYiOjEsImQiOjQ5NjJ9AAEAAAAAAWvQmxxQvUmRILHGE8_jPcUEO_0L_iwVULfsAGGCzo2tC4Mi

## [2026-08-10] Clarify Turmeric 20% product specification in vendor documentation

- Signal: repeated_question | Entity: F3E | observed 7x in the last 14 days
- Route: known_answer
- Recommendation: The question "Or is it just a generic Turmeric 20%?" appears 7 times in 14 days across the same thread, indicating ambiguity about product grade/sourcing that requires repeated clarification. Create a known-answer entry or vendor spec sheet that explicitly documents whether the quoted Turmeric 20% is a proprietary or generic formulation, including relevant differentiators (HPLC standards, sourcing, pricing tiers). This will prevent the same clarification from being requested repeatedly.
- Evidence: Or is it just a generic Turmeric 20%?; Or is it just a generic Turmeric 20%?

## [2026-08-17] Suppress custom field change notifications in work management system

- Signal: repeated_question | Entity: F3E | observed 7x in the last 14 days
- Route: known_answer
- Recommendation: F3E user is repeatedly encountering a question about email notification preferences for custom field updates, suggesting either unclear settings or a UX pain point. Create a known-answer entry or brief doc explaining how to disable these notifications in the work management system, or escalate to the platform vendor/admin to verify notification rules are correctly configured. This is a quick friction reducer for task management experience.
- Evidence: Don't want emails when work details (such as custom fields) change?; Don't want emails when work details (such as custom fields) change?

## [2026-08-17] Consolidate task notification routing for Core Power Yoga project

- Signal: repeated_question | Entity: F3E | observed 7x in the last 14 days
- Route: make_com
- Recommendation: F3E is receiving 7 duplicate task notifications in 14 days for the same Core Power Yoga task, suggesting misconfigured watchers or notification rules in Asana. Audit the task's follower list and notification settings to remove redundant alerts. If this is a systematic issue across projects, implement a Make.com automation to deduplicate notifications before they reach the inbox.
- Evidence: View task: https://app.asana.com/1/682743441507584/project/1211829207630075/task/1217284064094814?is_internal=false&focu; View task: https://app.asana.com/1/682743441507584/project/1211829207630075/task/1217284064094814?is_internal=false&focu

## [2026-08-17] Automate incomplete policy setup reminders with escalation logic

- Signal: repeated_question | Entity: BDM | observed 9x in the last 14 days
- Route: make_com
- Recommendation: Larry is receiving identical reminder emails 9 times in 14 days, suggesting a runaway automation or manual retry loop. Implement a Make.com scenario that tracks policy completion status and sends reminders with increasing intervals (e.g., day 1, day 3, day 7) rather than repeating daily. Add logic to escalate to Hannah after 3 failed attempts instead of continuing repetitive sends.
- Evidence: https://click.email.bristolwest.com/?qs=ABB7InYiOjEsImQiOjQ5Njl9AAEAAAAAAYBUb-yakHpaiMluwY8OFz9U9f6ltpx0CbCfxoq7sVXwuzIx; https://click.email.bristolwest.com/?qs=ABB7InYiOjEsImQiOjQ5Njl9AAEAAAAAAYBUb-ybckWfmsBdwN50KzJVwprC9IxI2OvTRMxnaUqkBvtT

## [2026-08-17] Reconcile Long Form Production pricing across BDM and F3E

- Signal: cross_entity_duplication | Entity: FNDR | observed 27x in the last 14 days
- Route: holdco_consolidation
- Recommendation: BDM offers Long Form Production at $900/day while F3E lists it at $2,000 as a fixed package. With 27 instances in 14 days, this pricing discrepancy is creating confusion and potential revenue leakage. Standardize pricing and service definitions across both entities at the holdco level, or clarify the scope differences (e.g., deliverables, revision rounds) that justify the 2.2x price gap.
- Evidence: Long Form Production 1 day - $900; Long Form Production - $2,000

## [2026-08-17] Standardize video short-form edit pricing across BDM and F3E

- Signal: cross_entity_duplication | Entity: FNDR | observed 27x in the last 14 days
- Route: holdco_consolidation
- Recommendation: BDM charges $250 for Video Short Form Edit while F3E's a la carte pricing shows $200 for the same service. With 27 instances in 14 days, this pricing inconsistency creates customer confusion and revenue leakage. Consolidate pricing and service definitions at HJR Global level to ensure consistent quoting and billing across entities.
- Evidence: Video Short Form Edit - $250; Video Short Form Edit - $200

## [2026-08-24] Automate monthly statement delivery routing to founder inbox

- Signal: repeated_question | Entity: FNDR | observed 17x in the last 14 days
- Route: make_com
- Recommendation: The founder is receiving identical monthly account statement notifications 17 times in 14 days, indicating a delivery or subscription duplication issue. This is a rule-based mechanical problem: either duplicate email rules are triggering, a vendor is resending, or the founder is subscribed multiple times to the same alert. A Make.com scenario can detect and deduplicate these messages, or route them to a single digest. Alternatively, consolidate the subscription at source to send one statement per month.
- Evidence: https://click.e.usa.experian.com/u/?qs=ABB7InYiOjEsImQiOjQ5NzZ9AAcAAAAABZoHXNodR6ChkYICtBFW4HxnSI4dh5IYEaLdJsQ8Uzq6a7JhE; https://click.e.usa.experian.com/u/?qs=ABB7InYiOjEsImQiOjQ5NzZ9AAcAAAAABZoHXNoenctfa-Bf2IgRQmVzE-iFWjNLH2EnbK8O-iSARnnvm

## [2026-08-24] Consolidate recurring policy management reminders into single reference doc

- Signal: repeated_question | Entity: FNDR | observed 10x in the last 14 days
- Route: doc
- Recommendation: The founder is receiving identical policy reminder emails 10 times in 14 days, indicating either a misconfigured automation or lack of a centralized tracking system. Create a single policy to-do reference document (checklist or dashboard) that Hannah Grant (Operations) can maintain and share, eliminating duplicate notifications. This is a low-lift doc that will reduce inbox clutter and clarify action items.
- Evidence: https://click.policy.farmers.com/?qs=ABB7InYiOjEsImQiOjQ5NzZ9AAEAAAAAAZXmhJUDSQwSGqu5ybweyOKIy9b6rHl2iKjb8kJWr1bWbkOUuB7; https://click.policy.farmers.com/?qs=ABB7InYiOjEsImQiOjQ5NzZ9AAEAAAAAAZXmhJUEez9Ha7TWwwUz_LjPFll8QK2kGyBm0hHWpht5UYpOq-R

## [2026-08-24] Automate Cox Business bill email routing to finance lead

- Signal: repeated_question | Entity: FNDR | observed 8x in the last 14 days
- Route: make_com
- Recommendation: The founder is receiving duplicate bill notification emails 8 times in 14 days, creating unnecessary inbox clutter. Set up an email filter or Make.com scenario to automatically forward Cox Business bill emails directly to Justin Moran (Controller/Finance Lead) and archive them from the founder's inbox. This is a rule-based mechanical automation that requires no language generation.
- Evidence: https://click.businesscontact.cox.com/?qs=ABB7InYiOjEsImQiOjQ5Nzd9AAwAAAAAAoCWF_tdsvCET7wqZvRDwRsg7l8lqqk1-LcRO9OhEwqu-k; https://click.businesscontact.cox.com/?qs=ABB7InYiOjEsImQiOjQ5Nzd9AAwAAAAAAoCWF_teF-56pQ-H9NZKR9HsE-jap9NrBUn9KxgWVphqSe

## [2026-08-24] Create known-answer entry for F3 13WCF case volume calculation

- Signal: repeated_question | Entity: FNDR | observed 4x in the last 14 days
- Route: known_answer
- Recommendation: The founder and operations manager have queried the same F3 case volume calculation 4 times in 14 days to determine units needed to meet the $562,952 forecast target. Create a documented known-answer entry or simple reference doc that shows the calculation logic (forecast amount ÷ price per case = required cases) so this can be self-served rather than recalculated repeatedly. Route to Hannah Grant (Ops) to own and maintain.
- Evidence: Looking at the 13WCF and using today's snapshot as of 3:24pm 8/19/2026, How many cases of F3 selling at $16.00 per case ; Looking at the 13WCF and using today's snapshot as of 3:24pm 8/19/2026, How many cases of F3 selling at $16.00 per case 

## [2026-08-24] Automate Cox Business bill retrieval and filing

- Signal: repeated_question | Entity: FNDR | observed 4x in the last 14 days
- Route: make_com
- Recommendation: The founder is receiving identical Cox Business bill notification emails 4x in 14 days, suggesting either duplicate subscriptions, a mail loop, or manual re-requests. Route bill retrieval through Make.com to automatically fetch and file statements on a monthly cadence, eliminating inbox clutter and manual clicks. Coordinate with Justin Moran (Controller) to confirm billing cadence and archive location.
- Evidence: https://click.businesscontact.cox.com/?qs=ABB7InYiOjEsImQiOjQ5Nzd9AAwAAAAAAoCWF_tf6_9ZdOXEfALHTlSZRAGwlv67_7FSn6rJOy4gla; https://click.businesscontact.cox.com/?qs=ABB7InYiOjEsImQiOjQ5Nzd9AAwAAAAAAoCWF_t5OTPK5UXCYzD5E0VWEK2khqNdxpBS-CmCNdfWVD
