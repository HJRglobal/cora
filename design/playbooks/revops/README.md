# Revenue-ops playbook templates

Silence-nudge bodies, per workstream. Seeded 2026-08-02 from B2's staged nudge
draft corpus (reply-watch-state.json), voice per
`_shared/playbooks/harrison-writing-style.md`.

Rules:
- The WHOLE file (minus nothing) is the template body. No front matter.
- Placeholders: `{first_name}` (counterparty first name, falls back to "there"),
  `{days_silent}` (integer). Rendering is deterministic string substitution;
  no LLM touches the nudge path.
- NEVER use an em-dash in a template (hard rule, locked 2026-07-31; the email
  egress guard blocks it anyway).
- Template changes merge through Harrison like any canon-adjacent config.
- File naming: `silence-nudge-<workstream>.md` where workstream is the
  lowercased canonical name (retail / press / suppliers); anything without a
  file falls back to `silence-nudge-default.md`.
