<!-- AUTHORITATIVE STORE: Drive _brain/known-answers (env KNOWN_ANSWERS_DIR). This repo copy is a DR seed / offline fallback ONLY -- the live bot reads Drive; do NOT hand-edit this expecting it to go live. See design/known-answers/README.md. -->

# Lexington Services -- Known Answers

_Compiled from Cora knowledge-gap reviews. Read alongside CLAUDE.md and founder CLAUDE.md. Append-only -- manually consolidate if it gets noisy._

## Routing rules

(empty - rules added as Harrison flags ROUTE: entries in digests)

## Known facts

### Lex sub-entity Manager assignments (LOCKED 2026-05-21)

Each Lex sub-entity has its own legal/operational Manager under AZ LLC law. Distinct from Shaun's role as overall Lex Services General Manager (he is ALSO the Manager of the LLC sub-entity specifically).

- **LLC (Lexington LLC)** -- Manager: **Shaun Hawkins** (Shaun@Lexingtonservices.com). Also overall Lex Services GM.
- **LTS (Lexington Therapy Services, LLC)** -- Manager: **Justin Gilmore** (justin.gilmore@lexingtonservices.com). Owns 80% of LTS via JG, LLC. Distinct from Justin Moran (HJR Global CFO). Address: 1337 S Gilbert Rd, Suite 105, Mesa AZ 85204.
- **LBHS (Lexington Behavioral Health Services)** -- Manager: **Jared Harker**. Also LBHS 75% majority owner via HMLA LLC (acquired 8/1/2025 for $121,859.20).
- **LLA (Lex Life Academy)** -- Manager: **Sandy Patel**. Operational Manager despite no longer being a direct member (10% repurchased 2023-08-16). Has a separate Services Agreement with LLA on file. Co-owner of SBP Inc. (Patel family AZ corp) with Bryan Patel.

For sub-entity-specific operational questions, the sub-entity Manager is the right point of contact. Shaun coordinates across all four as overall Lex Services GM. Harrison is sole authority on access / money / contracts / comms decisions across all sub-entities (per 2026-05-21 authority doctrine -- Managers execute within their lane, they are not approval gates).

### AZ DDD Therapy Revalidation (LOCKED 2026-05-31)

- **Hard deadline:** 2026-06-30. No extension available.
- **What gets terminated:** AHCCCS Provider Type 15 service-site IDs across all Lexington service locations. Provider Type 15 = Therapy. Termination means service delivery stops -- AHCCCS will not reimburse for services billed under lapsed IDs.
- **Revenue risk:** Material. Loss of AHCCCS billing authorization across active therapy service sites. Revalidation paperwork is in progress.
- **Contact:** tguzman@azdes.gov, Arizona Department of Economic Security (AZ DES). Original notification sent 2026-05-21 to Harrison and Shaun.
- **Asana task:** GID `1215070649606664`. Owner: Harrison. Operational executor: Shaun Hawkins (LLC side coordination). Justin Gilmore (LTS) is the primary executor for LTS-specific revalidation steps.
- **Current status:** Revalidation paperwork in progress as of 2026-05-22. Shaun coordinates on the LLC/LLC-adjacent side; Justin Gilmore drives LTS steps.
- **Days to deadline from 2026-05-31:** 30 days. Call `lex_revalidation_status` for live countdown and open blockers -- do not answer from this static entry.

### How to get a document or a correction into Cora (LOCKED 2026-08-06)

Answers the recurring "what happens to what I send you" question (BOTH-03; three
people asked on the 8/6 calls, twice cut off mid-answer). This is the mechanism,
stated plainly:

- **Documents -> the Lexington Drive dump folder.** That is the supported path
  and the only one. A nightly sync (4:45am AZ) indexes whatever is in it, so a
  file dropped today is answerable tomorrow morning -- not instantly.
- **Cora does NOT browse Drive live** and cannot open a link pasted into Slack.
  If a document has not been through the nightly sync, she does not have it yet.
- **There is no email-in address.** Do not tell anyone to email a document to
  Cora -- no such mailbox is monitored (LEX-13: this was promised once and the
  document never arrived). Teammates' own Lexington mailboxes ARE swept
  nightly, so mail they send or receive is picked up that way, but that is a
  side effect, not an intake path to rely on.
- **Corrections** ("that's wrong, it's actually X") go into a review queue for
  Harrison to approve. They are NOT live the moment they are said. Cora
  acknowledges, queues, and Harrison's approval is what makes it canonical.
- **"Send it to Harrison" is not a ticket.** It is a message to a person --
  nothing is tracked or scheduled by it. If something must be tracked, it needs
  an Asana task.
- **Manuals already indexed:** the DDD provider manuals (Complete Provider,
  Operations, Medical, Behavior Supports, Eligibility) and the EVV document set
  are in the knowledge base -- ask policy questions directly rather than
  re-sending the PDFs.
