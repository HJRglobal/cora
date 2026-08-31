"""Write-sentinel contract rails (session #11 S1).

Two hand-maintained frozensets decide whether a write tool's TRUTHFUL success
narration survives (_CONTRACT_WRITE_TOOLS -> narration net posts the tool text
verbatim; _NON_SENTINEL_WRITE_TOOLS -> phantom guard stands down). Neither was
pinned to the place write tools are actually registered, so 10 tools sat outside
both for months and a real "Done." on their confirm turn was replaced with
"I didn't actually change anything in Asana just now" (cq-b75ff2802764).

D-232: enumerate against the REAL registration point, never a hand list.
"""
import ast
import io
import re

from src.cora.claude_client import (
    _CONTRACT_WRITE_TOOLS,
    _NON_SENTINEL_WRITE_TOOLS,
    _PHANTOM_CONFIRM_CLAIM_RE,
    _PHANTOM_CONFIRM_MAX_LEN,
    _PHANTOM_DESTRUCTIVE_CORRECTION,
    _guard_phantom_destructive,
    _should_broaden,
)

_TOOL_DISPATCH_SRC = "src/cora/tools/tool_dispatch.py"


def _staged_write_tools() -> set[str]:
    """The registration point: a staged-write tool is one whose TOOL_DEFINITIONS
    input_schema carries `confirmed` (doctrine 1, the staged-write gate).

    Parsed with ast so the roster is read from the DEFINITION, not from whatever
    a runtime import happens to have mutated.
    """
    tree = ast.parse(io.open(_TOOL_DISPATCH_SRC, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TOOL_DEFINITIONS":
                    defs = ast.literal_eval(node.value)
                    return {
                        d["name"]
                        for d in defs
                        if "confirmed" in ((d.get("input_schema") or {}).get("properties") or {})
                    }
    raise AssertionError("TOOL_DEFINITIONS not found in %s" % _TOOL_DISPATCH_SRC)


class TestRegistrationPointPin:
    def test_every_staged_write_tool_is_in_exactly_one_set(self):
        """The invariant. A new staged-write tool must be classified deliberately:
        either the narration net posts its payload verbatim (_CONTRACT_WRITE_TOOLS)
        or the phantom guard must stand down for it (_NON_SENTINEL_WRITE_TOOLS).
        Landing in NEITHER is the bug this test exists to prevent."""
        staged = _staged_write_tools()
        classified = _CONTRACT_WRITE_TOOLS | _NON_SENTINEL_WRITE_TOOLS
        unclassified = staged - classified
        assert not unclassified, (
            "staged-write tool(s) in neither set -- a truthful success narration on "
            "their confirm turn can be clobbered by the phantom guard: %s"
            % sorted(unclassified)
        )

    def test_no_tool_is_in_both_sets(self):
        assert not (_CONTRACT_WRITE_TOOLS & _NON_SENTINEL_WRITE_TOOLS)

    def test_neither_set_names_a_non_staged_write_tool(self):
        """Guards the other direction: a READ tool in _NON_SENTINEL_WRITE_TOOLS
        would silently disarm the phantom backstop (re-verify MED)."""
        staged = _staged_write_tools()
        stray = (_CONTRACT_WRITE_TOOLS | _NON_SENTINEL_WRITE_TOOLS) - staged
        assert not stray, "not staged-write tools: %s" % sorted(stray)

    def test_counts_are_pinned(self):
        """Tripwire so a roster change is a deliberate edit, not a drift."""
        assert len(_staged_write_tools()) == 22
        assert len(_CONTRACT_WRITE_TOOLS) == 8
        assert len(_NON_SENTINEL_WRITE_TOOLS) == 14


class TestTheLiveDefect:
    """The four tools that fell through BOTH controls. Regression pins."""

    EXPOSED = [
        "cora_remember",
        "cora_forget_note",
        "cora_lexicon_add",
        "cora_queue_code_session",
    ]

    def test_exposed_tools_now_stand_the_guard_down(self):
        for name in self.EXPOSED:
            assert name in _NON_SENTINEL_WRITE_TOOLS
            assert _should_broaden(True, {"tool_names": [name]}) is False, name

    def test_terse_success_narrations_were_being_clobbered(self):
        """Documents the defect: these are the narrations the exposed tools emit,
        and every one matches the broadened phantom regex under the length cap."""
        for text in ("Done.", "Deleted it.", "Created it.", "Done -- note deleted."):
            assert len(text) <= _PHANTOM_CONFIRM_MAX_LEN
            assert _PHANTOM_CONFIRM_CLAIM_RE.search(text), text
            # With broaden ON (the pre-fix state) a TRUE success became a denial.
            assert _guard_phantom_destructive(text, broaden=True) == _PHANTOM_DESTRUCTIVE_CORRECTION
            # With the tool correctly classified, broaden is False and the truth survives.
            assert _guard_phantom_destructive(text, broaden=False) == text

    def test_phantom_backstop_still_fires_when_no_write_tool_ran(self):
        """The fix must not disarm the guard on its actual target: a bare
        affirmative with NO write tool this turn is still a fabrication."""
        assert _should_broaden(True, {"tool_names": []}) is True
        assert _should_broaden(True, {"tool_names": ["asana_get_my_tasks"]}) is True
        assert (
            _guard_phantom_destructive("Done -- task deleted.", broaden=True)
            == _PHANTOM_DESTRUCTIVE_CORRECTION
        )


class TestAssumeConfirmGateCoversEveryPendingKind:
    """app.py's assume_confirm gate is the OTHER half. It enumerated five pending
    kinds while the Sonnet gate below it enumerated nine; the four in the second
    list but not the first are exactly the tools that were exposed."""

    def test_gate_probes_every_staged_write_pending_kind(self):
        src = io.open("src/cora/app.py", encoding="utf-8").read()
        # the assume_confirm assignment block
        m = re.search(r"assume_confirm = \(\n(.*?)\n    \)\n", src, re.S)
        assert m, "assume_confirm block not found"
        block = m.group(1)
        for probe in (
            "has_pending_asana_write",
            "has_pending_shopify_write",
            "has_pending_calendar_write",
            "has_pending_delegated_write",
            "has_pending_classb",
            "has_pending_remember",
            "has_pending_forget_note",
            "has_pending_lexicon",
            "has_pending_code_queue",
            "has_pending_schedule_meeting",
        ):
            assert probe in block, "assume_confirm gate is blind to %s" % probe

    def test_lexicon_probe_exists(self):
        """cora_lexicon_add was the one staged-write kind with no has_pending_*
        wrapper at all, so the gate could not have seen it even if listed."""
        from src.cora.tools import tool_dispatch

        assert callable(tool_dispatch.has_pending_lexicon)
        assert tool_dispatch.has_pending_lexicon("U_NOBODY", "nochannel") is False


class TestEgressScrub:
    """The post-seam strip. reply_formatter's docstring claimed app.py did this;
    app.py never contained the token. The scrub lives at the universal egress
    boundary so it also covers the paths that never reach format_reply."""

    def test_ordinary_text_is_byte_identical(self):
        """The module's hard-won prohibition: transforms here must be safe on
        arbitrary content. No sentinel -> the SAME OBJECT comes back."""
        from src.cora.slack_egress import scrub_write_sentinels

        for text in (
            "Here are your 3 open tasks:\n- *Fix it* (due Mon)",
            "Cash: $1,347,657  |  OSN $77,629",  # double space preserved
            "```\ncode fence  with  spacing\n```",
            "The QuickBooks sync failed -- see <https://x.co|link>",
        ):
            assert scrub_write_sentinels(text) is text, text

    def test_leading_sentinel_directive_is_removed(self):
        from src.cora.slack_egress import scrub_write_sentinels

        out = scrub_write_sentinels("WRITE_CONFIRMED: Updated inventory to 42.")
        assert "WRITE_CONFIRMED" not in out
        assert "Updated inventory to 42." in out

    def test_midsentence_token_is_removed(self):
        from src.cora.slack_egress import scrub_write_sentinels

        out = scrub_write_sentinels("The tool returned WRITE_CONFIRMED so it is done.")
        assert "WRITE_CONFIRMED" not in out
        assert out.startswith("The tool returned")

    def test_sentinel_only_body_is_never_fabricated_over(self):
        """Scrubbing to empty would break the send. We return the original and
        log at ERROR rather than invent prose -- inventing is what makes
        tool_dispatch._strip_write_sentinel unsafe at this boundary."""
        from src.cora.slack_egress import scrub_write_sentinels

        assert scrub_write_sentinels("WRITE_CONFIRMED") == "WRITE_CONFIRMED"

    def test_scrub_runs_inside_sanitize_text(self):
        from src.cora.slack_egress import sanitize_text

        assert "WRITE_CONFIRMED" not in sanitize_text("WRITE_CONFIRMED: done.")

    def test_sentinel_list_matches_claude_client(self):
        """Drift guard: two modules name the same tokens."""
        from src.cora.claude_client import _SHOPIFY_SENTINELS
        from src.cora.slack_egress import _WRITE_SENTINELS

        assert set(_WRITE_SENTINELS) == set(_SHOPIFY_SENTINELS)

    def test_mode_defaults_to_observe(self, monkeypatch):
        from src.cora import slack_egress

        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        assert slack_egress._sentinel_mode() == "observe"
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        assert slack_egress._sentinel_mode() == "enforce"

    def test_scrub_is_on_in_both_modes(self, monkeypatch):
        """The flag governs loudness, never whether the token is allowed through."""
        from src.cora import slack_egress

        for mode in ("observe", "enforce"):
            monkeypatch.setenv("CORA_SENTINEL_ENFORCE", mode)
            out = slack_egress.scrub_write_sentinels("WRITE_CONFIRMED: ok.")
            assert "WRITE_CONFIRMED" not in out, mode
