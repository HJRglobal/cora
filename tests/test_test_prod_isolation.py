"""Test/prod isolation rail (session #11 S2, cq-eba0861fc043 HIGH).

THE INCIDENT. tests/conftest.py redirects a list of write paths to tmp. One
constant -- knowledge_review._PROPOSED_UPDATES_PATH -- was never on that list,
though its SIBLING _AUTOWRITE_AUDIT_PATH was. propose_update() appends to it
unconditionally, so a suite run put two synthetic "gapfill-*" proposals into the
LIVE review ledger; one was later one-tapped and written into live canon.

WHY THE EXISTING BELT DID NOT CATCH IT. The file WAS listed in _GUARDED_LEDGERS,
i.e. detect-only, and the detector is structurally defeated on this host:
_guard_logs_untouched downgrades a real-ledger mutation from AssertionError to
warnings.warn whenever _bot_live() is true, and on an always-on host it always is.
A genuine isolation escape is therefore MASKED as the same warning the live bot
produces. Detection cannot be the primary control here -- REDIRECTION is.

WHY THIS TEST EXISTS. It was not a one-off: the identical unredirected constant
took a lexicon-teach fixture on 2026-08-02, three weeks earlier, unnoticed. And a
previous fix for this same CLASS shipped in July (cq-d9432f552a33, "test fixture
text leaks into live _brain/known-answers/f3e.md") -- it closed one writer while
the hole reopened at another. A point-fix per incident does not converge; the
surface has to be ENUMERATED, so every new write path is classified deliberately.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

_SRC = Path("src/cora")
_CONFTEST = Path("tests/conftest.py")

# Path-shaped env vars resolved at call time inside src/cora.
# Suffixes that name a filesystem location. LEDGER/LOG/FILE were added in
# session #11 after this very rail MISSED a new write path introduced in the same
# session (run_marker's TASK_RUNS_LEDGER): a completeness rail keyed on a naming
# convention silently under-reports every variable that does not follow it. The
# var was also renamed to TASK_RUNS_LEDGER_PATH to match the repo's convention --
# both halves, because either alone leaves the hole open for the next one.
_ENV_PATH_RE = re.compile(
    r"""os\.environ\.get\(\s*["']([A-Z0-9_]*(?:PATH|DIR|ROOT|LEDGER|LOG|FILE))["']"""
)
_SETENV_RE = re.compile(r"""monkeypatch\.setenv\(\s*["']([A-Z0-9_]+)["']""")

# Env-var path resolvers deliberately NOT redirected, each with the reason.
# A new entry here is a DECLARATION, not a shrug -- if it names a WRITE target,
# redirect it instead. Keeping the list explicit is the whole point: before this
# test the unredirected set was invisible, which is how one of them reached canon.
_DECLARED_UNREDIRECTED: dict[str, str] = {
    # --- read-only inputs: seed/config data a test legitimately reads ---
    "BRAIN_PEOPLE_DIR": "read-only: person dossier source files",
    "DYNAMIC_ANSWERS_DIR": "read-only: dynamic-answer templates (dormant, D-085)",
    "GAP_DOMAIN_OWNERS_PATH": "read-only: gap-domain-owners map",
    "LEXICON_DIR": "read-only: lexicon seed yaml; redirecting empties the seeds tests read",
    "LEXICON_SKU_ALIASES_PATH": "read-only: SKU alias seed map",
    "LEXICON_USER_ALIASES_PATH": "read-only: user alias seed map",
    "STRATEGY_ASANA_MAP_PATH": "read-only: slack-to-asana map",
    "STRATEGY_DECISIONS_PATH": "read-only: decisions-pending.md has NO writer in src/ "
                               "(decision_alerts/decision_lane both read it)",
    "KB_DECISION_LOG_PATH": "read-only: decision log scanned for gap detection",
    # --- database handles: opened read-only or against a tmp db the test supplies ---
    "CORA_KB_DB_PATH": "db handle; tests pass their own path or use a tmp KB",
    "FRICTION_KB_DB_PATH": "db handle; friction mining tests supply a tmp db",
    "MATERIALIZER_KB_DB_PATH": "db handle; materializer tests supply a tmp db",
    "STRATEGY_KB_DB_PATH": "db handle; strategy tests supply a tmp db",
    # --- external roots: already neutralized by CORA_DRIVE_ROOT / not written in tests ---
    "FOUNDER_OS_ROOT": "Drive root; the write-side is covered by CORA_DRIVE_ROOT",
    "SWEPT_DIR": "Drive-side swept corpus root, read-side only",
    "CLAUDE_PROJECTS_ROOT": "read-only: session-capture harvest source",
    "COWORK_SESSIONS_ROOT": "read-only: session-capture harvest source",
}


def _env_path_vars() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for f in _SRC.rglob("*.py"):
        text = io.open(f, encoding="utf-8").read()
        for m in _ENV_PATH_RE.finditer(text):
            found.setdefault(m.group(1), set()).add(f.as_posix())
    return found


def _redirected_env_vars() -> set[str]:
    return set(_SETENV_RE.findall(io.open(_CONFTEST, encoding="utf-8").read()))


class TestEveryWritePathIsClassified:
    def test_no_unclassified_env_path_resolver(self):
        """The rail. Every path-shaped env var in src/cora is either redirected to
        tmp by the autouse fixture, or explicitly declared read-only above."""
        found = _env_path_vars()
        redirected = _redirected_env_vars()
        unclassified = {
            k: sorted(v) for k, v in found.items()
            if k not in redirected and k not in _DECLARED_UNREDIRECTED
        }
        assert not unclassified, (
            "unclassified path env var(s) -- redirect them in tests/conftest.py, or add "
            "them to _DECLARED_UNREDIRECTED with a reason if they are read-only:\n%s"
            % "\n".join("  %s  <- %s" % (k, ", ".join(v)) for k, v in sorted(unclassified.items()))
        )

    def test_declared_list_has_no_stale_entries(self):
        """A declared entry whose env var no longer exists in src/ is drift --
        it makes the list look more considered than it is."""
        found = set(_env_path_vars())
        stale = sorted(set(_DECLARED_UNREDIRECTED) - found)
        assert not stale, "declared but no longer used in src/cora: %s" % stale

    def test_declared_and_redirected_do_not_overlap(self):
        overlap = sorted(set(_DECLARED_UNREDIRECTED) & _redirected_env_vars())
        assert not overlap, "both redirected AND declared read-only: %s" % overlap


class TestTheKnowledgeReviewLedgerHole:
    """The specific constants that let a fixture reach canon."""

    def test_review_ledger_constants_are_redirected(self):
        conftext = io.open(_CONFTEST, encoding="utf-8").read()
        for const in ("_PROPOSED_UPDATES_PATH", "_ARCHIVE_PATH", "_REPLY_LOG_PATH"):
            assert const in conftext, (
                "knowledge_review.%s is not redirected in conftest -- this is the exact "
                "constant that put test fixtures into live canon (cq-eba0861fc043)" % const
            )

    def test_propose_update_writes_to_tmp_not_the_repo(self, tmp_path):
        """Behavioural proof, not a string check: a propose_update() during a test
        must not touch the real data/cora-proposed-memory-updates.jsonl."""
        from cora import knowledge_review

        target = Path(knowledge_review._PROPOSED_UPDATES_PATH)
        assert "cora-proposed-memory-updates.jsonl" == target.name
        # The autouse fixture must have moved it off the repo tree entirely.
        repo_copy = Path("data") / "cora-proposed-memory-updates.jsonl"
        assert target.resolve() != repo_copy.resolve(), (
            "propose_update would write to the LIVE review ledger at %s" % repo_copy
        )

    def test_archive_and_reply_log_also_moved(self):
        from cora import knowledge_review

        for attr, real in (
            ("_ARCHIVE_PATH", Path("data") / "cora-proposed-memory-updates.archive.jsonl"),
            ("_REPLY_LOG_PATH", Path("data") / "cora-reply-log.jsonl"),
        ):
            got = Path(getattr(knowledge_review, attr))
            assert got.resolve() != real.resolve(), "%s still points at %s" % (attr, real)


class TestGuardIsNotThePrimaryControl:
    def test_bot_live_downgrade_is_documented(self):
        """_guard_logs_untouched degrades to a warning while the bot is live, so it
        can never fail a run on Harrison's always-on host. This test does not change
        that (the downgrade prevents constant false failures from concurrent real
        bot writes) -- it pins the fact that the guard is a BACKSTOP, so nobody
        later mistakes a green run for proof of isolation."""
        conftext = io.open(_CONFTEST, encoding="utf-8").read()
        assert "_bot_live" in conftext
        assert "warnings.warn" in conftext, (
            "if the downgrade is ever removed, revisit this test and the S2 report"
        )
