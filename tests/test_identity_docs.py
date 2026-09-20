"""Code #13 RIDER 1 slice S-C -- identity inventory + doc hygiene pins.

Docs-only slice (no runtime change). These tests pin:
  * the four new runbook sections exist exactly once, sit adjacent to
    ``## Rotating Tokens``, are ASCII (D-016 posture for anything a PowerShell
    operator may paste), and carry no secret-shaped VALUE;
  * the Identity inventory names every credential-shaped key in ``.env.example``
    (a drift guard: a new credential key needs an inventory row);
  * the DWD section names every scope the CODE requests (drift guard #2);
  * ``SLACK_USER_TOKEN`` is recorded ABSENT + do-not-add, and no ``src/`` module
    reads it;
  * the bootstrap doc points at the inventory; the CLAUDE.md founder-id line is
    untouched with the dated NOTE directly under it;
  * ``scripts/probe_slack_user_ids.py`` imports without network, calls ONLY
    ``users_info``, never prints the token, and its id regex is linear.

Only the NEW sections are pinned ASCII: the rest of ``runbook.md`` /
``bootstrap-new-machine.md`` predates the rule and carries em-dashes.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_RUNBOOK = _REPO / "deployment" / "runbook.md"
_BOOTSTRAP = _REPO / "deployment" / "bootstrap-new-machine.md"
_CLAUDE_MD = _REPO / "CLAUDE.md"
_ENV_EXAMPLE = _REPO / ".env.example"
_PROBE = _REPO / "scripts" / "probe_slack_user_ids.py"
_CAPTURE_ROSTER = _REPO / "data" / "maps" / "meeting-capture-roster.yaml"

SECTIONS = (
    "## Identity inventory",
    "## Rotation: Asana PAT (Cora)",
    "## Provisioning: cora@hjrglobal.com",
    "## Provisioning: Google service account + DWD",
)

# Dummy token shapes for the probe tests, assembled by concatenation so this
# file itself never carries a literal that the kickoff secrets scan (xox[abp]-)
# over the branch diff would flag.
_DUMMY_BOT_TOKEN = "".join(("xox", "b-test-dummy-token-for-ci"))
_DUMMY_LEAKY_ERROR = "invalid_auth for token " + "".join(("xox", "b-123456789012-abcdefghijkl"))
_BOT_PREFIX = _DUMMY_BOT_TOKEN[:5]  # the 5-char prefix every real bot token starts with

# The kickoff section 4 item 5 scan (Slack / Asana / Google / OpenAI-Anthropic
# shapes). The Google and OpenAI prefixes are split so this source line is not
# itself a hit when the same scan runs over the branch diff.
_SECRET_SHAPES = (
    re.compile(r"xox[abp]-[A-Za-z0-9-]{8,}"),
    re.compile(r"2/[0-9]{10,}/"),
    re.compile("AI" + "za[0-9A-Za-z_-]{20,}"),
    re.compile("sk" + "-[A-Za-z0-9_-]{16,}"),
)


def _runbook() -> str:
    return _RUNBOOK.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of one '## ' section: heading through the char before the next '## '."""
    assert text.count("\n" + heading + "\n") + text.count("\n" + heading + "\r\n") == 1, heading
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start:] if nxt == -1 else text[start:nxt]


def _identity_sections() -> dict[str, str]:
    text = _runbook()
    return {h: _section(text, h) for h in SECTIONS}


# -- runbook: existence, placement, ASCII, no values --

def test_runbook_has_each_identity_section_exactly_once():
    text = _runbook()
    for h in SECTIONS:
        assert len(re.findall(rf"^{re.escape(h)}\s*$", text, flags=re.M)) == 1, h


def test_identity_sections_sit_adjacent_to_rotating_tokens():
    text = _runbook()
    rot = text.index("## Rotating Tokens")
    inv = text.index(SECTIONS[0])
    nxt = text.index("## Updating Channel Routing")
    assert rot < inv < nxt, "inventory must follow Rotating Tokens and precede Updating Channel Routing"
    # the four sections run in the documented order, contiguous
    idx = [text.index(h) for h in SECTIONS]
    assert idx == sorted(idx)
    assert idx[-1] < nxt


def test_identity_sections_are_ascii():
    for h, body in _identity_sections().items():
        bad = [(i, c) for i, c in enumerate(body) if ord(c) > 127]
        assert not bad, f"{h}: non-ASCII at {bad[:3]}"


def test_identity_sections_carry_no_secret_shaped_values():
    for h, body in _identity_sections().items():
        for rx in _SECRET_SHAPES:
            assert not rx.search(body), f"{h}: secret-shaped text matched {rx.pattern}"


def test_identity_sections_never_carry_an_env_assignment_with_a_value():
    """Key NAMES only: a 'KEY=' followed by anything but a blank/placeholder is a leak."""
    rx = re.compile(r"^[A-Z][A-Z0-9_]+=(\S+)", re.M)
    for h, body in _identity_sections().items():
        for m in rx.finditer(body):
            assert m.group(1) in {"cora", "harrison"} or m.group(1).startswith("____"), (
                f"{h}: env assignment with a value: {m.group(0)!r}"
            )


def test_inventory_carries_the_d308_doctrine_line():
    inv = _identity_sections()[SECTIONS[0]]
    for phrase in (
        "admin-level only where the platform's API model requires it",
        "Fireflies precedent",
        "credential inside the code seam",
        "Harrison-provisioned",
        "listed here",
    ):
        assert phrase in inv, phrase


# -- drift guard 1: every credential key in .env.example is in the inventory --

_CRED_SUFFIX = re.compile(r"(_TOKEN|_KEY|_SECRET|_PAT|_PAT_CORA|_PASS|_JSON|_WEBHOOK_URL)$")


def _env_example_credential_keys() -> set[str]:
    keys = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#?\s?([A-Z][A-Z0-9_]+)=", line)
        if m and _CRED_SUFFIX.search(m.group(1)):
            keys.add(m.group(1))
    assert keys, ".env.example parsed no credential keys -- test broken"
    return keys


def test_inventory_names_every_credential_key_in_env_example():
    inv = _identity_sections()[SECTIONS[0]]
    missing = sorted(k for k in _env_example_credential_keys() if f"`{k}`" not in inv)
    assert not missing, f"credential keys in .env.example with no inventory row: {missing}"


def test_inventory_records_slack_user_token_absent_and_not_added():
    inv = _identity_sections()[SECTIONS[0]]
    assert "`SLACK_USER_TOKEN`" in inv
    assert "ABSENT from the live `.env`" in inv
    assert "Do NOT add the key anywhere" in inv
    # the recorded fallback fact is the LIVE one, not the kickoff's "fails if run"
    assert "FALLS BACK to `SLACK_BOT_TOKEN`" in inv
    # and no bot module reads it (script-only stays true)
    readers = [p for p in (_REPO / "src").rglob("*.py") if "SLACK_USER_TOKEN" in p.read_text(encoding="utf-8", errors="ignore")]
    assert readers == [], f"src/ modules now read SLACK_USER_TOKEN: {readers}"
    # exactly one documented line in .env.example, none added
    ex = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert len(re.findall(r"^SLACK_USER_TOKEN=", ex, flags=re.M)) == 1


def test_inventory_marks_fireflies_as_the_only_admin_identity():
    inv = _identity_sections()[SECTIONS[0]]
    rows = [l for l in inv.splitlines() if l.startswith("| ") and "YES" in l.split("|")[5]]
    assert len(rows) == 1 and rows[0].startswith("| Fireflies "), rows


# -- rotation section --

def test_rotation_section_names_the_flip_selector_and_retention():
    rot = _identity_sections()[SECTIONS[1]]
    for token in ("`CORA_ASANA_IDENTITY`", "`ASANA_PAT_CORA`", "`ASANA_PAT`", "14", "HARD FAIL"):
        assert token in rot, token
    assert "removed from .env and revoked in Asana on: ____-__-__" in rot
    assert "cq-40baab26d7f3" in rot
    # the health-check guard is a live fact -- and S-B changed it in the SAME rider:
    # the check no longer reads ASANA_PAT directly; it asks the resolver for the
    # ACTIVE identity's key, so removing Harrison's key on day 14 (after the flip)
    # keeps the check green. Pin the post-S-B state the runbook now describes.
    hc = (_REPO / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
    assert 'os.environ.get("ASANA_PAT"' not in hc
    assert "asana_identity" in hc
    assert "identity-aware" in rot


# -- cora@ provisioning --

def test_cora_provisioning_cross_references_the_capture_roster():
    sec = _identity_sections()[SECTIONS[2]]
    assert "meeting-capture-roster.yaml" in sec and "capture_identity" in sec
    roster = yaml.safe_load(_CAPTURE_ROSTER.read_text(encoding="utf-8"))
    assert roster["capture_identity"] == "cora@hjrglobal.com"
    for phrase in ("NO Gmail delegates", "Google account switcher", "no admin role", "fireflies_seat: false", "intake_route"):
        assert phrase in sec, phrase


def test_cora_provisioning_states_no_credential_for_the_mailbox():
    sec = _identity_sections()[SECTIONS[2]]
    assert "No password and" in sec and "no per-user credential for cora@" in sec


# -- drift guard 2: DWD scopes named in the section == scopes the code requests --

_SCOPE_RE = re.compile(r"googleapis\.com/auth/([a-z][a-z.]*[a-z])")


def _code_scopes() -> set[str]:
    found = set()
    for root in ("src", "scripts"):
        for p in (_REPO / root).rglob("*.py"):
            for m in _SCOPE_RE.finditer(p.read_text(encoding="utf-8", errors="ignore")):
                found.add(m.group(1))
    assert found, "no scope strings found in code -- test broken"
    return found


def test_dwd_section_names_every_scope_the_code_requests():
    sec = _identity_sections()[SECTIONS[3]]
    named = set(re.findall(r"^\| `([a-z.]+)`", sec, flags=re.M))
    missing = sorted(_code_scopes() - named)
    assert not missing, f"scopes requested in code but absent from the runbook table: {missing}"


def test_dwd_section_states_the_character_for_character_rule_and_the_withheld_scopes():
    sec = _identity_sections()[SECTIONS[3]]
    assert "character-for-character" in sec
    assert "2026-08-26" in sec and "D-245" in sec
    assert "`gmail.send`" in sec and "WITHHELD" in sec and "`spreadsheets` (write)" in sec
    assert "`GOOGLE_SERVICE_ACCOUNT_JSON`" in sec and "`client_id` field INSIDE" in sec


# -- bootstrap pointer + CLAUDE.md note --

def test_bootstrap_points_to_the_inventory_under_not_covered():
    text = _BOOTSTRAP.read_text(encoding="utf-8")
    start = text.index("## What's NOT covered by this runbook")
    end = text.index("## Sanity check questions")
    block = text[start:end]
    assert "Identity inventory in `deployment/runbook.md`" in block
    assert text.count("Identity inventory in `deployment/runbook.md`") == 1
    pointer = [l for l in block.splitlines() if "Identity inventory" in l]
    assert len(pointer) == 1 and pointer[0].isascii()


def test_claude_md_founder_id_line_untouched_with_the_dated_note_under_it():
    lines = _CLAUDE_MD.read_text(encoding="utf-8").splitlines()
    idx = [i for i, l in enumerate(lines) if l == "  Harrison (founder):       U02P3D6AT2C"]
    assert len(idx) == 1, "the founder id line must be present exactly once and unedited"
    note = lines[idx[0] + 1]
    assert note.startswith("  NOTE 2026-09-19")
    for token in ("UNDER VERIFICATION", "U0B2RM2JYJ1", "scripts/probe_slack_user_ids.py", "Do not edit the id until then"):
        assert token in note, token
    assert note.isascii()
    # the cited code anchors are live symbols
    td = (_REPO / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(encoding="utf-8")
    assert '_FOUNDER_SLACK_ID = "U0B2RM2JYJ1"' in td and '_HARRISON_SLACK_ID = "U0B2RM2JYJ1"' in td
    assert '_HARRISON_ID = "U0B2RM2JYJ1"' in (_REPO / "src" / "cora" / "user_access.py").read_text(encoding="utf-8")
    assert '_FOUNDER_ID = "U0B2RM2JYJ1"' in (_REPO / "src" / "cora" / "review_lanes.py").read_text(encoding="utf-8")


# -- the probe script --

def _load_probe():
    try:
        sys.path.insert(0, str(_REPO / "scripts"))
        import probe_slack_user_ids as m
        return m
    except ImportError:
        pytest.skip("probe_slack_user_ids not importable")


class _FakeClient:
    """Records every method name called; answers users_info from a table."""

    def __init__(self, table: dict[str, dict | Exception]):
        self.table = table
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        def _call(**kw):
            self.calls.append((name, kw))
            if name != "users_info":
                raise AssertionError(f"probe called a non-users_info method: {name}")
            uid = kw.get("user")
            if uid not in self.table:
                raise RuntimeError("The request to the Slack API failed. (error: user_not_found)")
            ans = self.table[uid]
            if isinstance(ans, Exception):
                raise ans
            return {"ok": True, "user": ans}
        return _call


def test_probe_imports_without_network_and_without_slack_sdk_at_import(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _guard(name, *a, **k):
        if name.startswith("slack_sdk"):
            raise AssertionError("slack_sdk imported at module import time")
        return real_import(name, *a, **k)

    sys.modules.pop("probe_slack_user_ids", None)
    monkeypatch.setattr(builtins, "__import__", _guard)
    m = _load_probe()
    assert m.DEFAULT_IDS == ("U02P3D6AT2C", "U0B2RM2JYJ1")


def test_probe_calls_only_users_info_once_per_id_and_prints_only_name_flags():
    m = _load_probe()
    fake = _FakeClient({
        "U02P3D6AT2C": {"real_name": "Old Harrison", "deleted": True, "is_bot": False},
        "U0B2RM2JYJ1": {"real_name": "Harrison Rogers", "deleted": False, "is_bot": False},
    })
    rows = m.probe(m.DEFAULT_IDS, fake)
    assert [c[0] for c in fake.calls] == ["users_info", "users_info"]
    assert [c[1] for c in fake.calls] == [{"user": "U02P3D6AT2C"}, {"user": "U0B2RM2JYJ1"}]
    out = m.render(rows)
    assert "U0B2RM2JYJ1 -> real_name='Harrison Rogers' deleted=False is_bot=False" in out
    assert "U02P3D6AT2C -> real_name='Old Harrison' deleted=True" in out
    assert "ANSWER: U0B2RM2JYJ1 is the live human" in out
    assert m.exit_code(rows) == 0
    assert set(rows[1]) == {"id", "real_name", "deleted", "is_bot"}  # no extra profile fields leak


def test_probe_escalates_when_both_ids_resolve_to_live_humans():
    m = _load_probe()
    fake = _FakeClient({uid: {"real_name": "Someone", "deleted": False, "is_bot": False} for uid in m.DEFAULT_IDS})
    rows = m.probe(m.DEFAULT_IDS, fake)
    assert "BOTH RESOLVE -- ESCALATE" in m.render(rows)
    assert m.exit_code(rows) == 3


def test_probe_scrubs_token_shaped_text_from_api_errors_and_never_raises():
    m = _load_probe()
    fake = _FakeClient({"U0B2RM2JYJ1": RuntimeError(_DUMMY_LEAKY_ERROR)})
    rows = m.probe(["U0B2RM2JYJ1", "not-an-id"], fake)
    assert _BOT_PREFIX not in rows[0]["error"] and "<redacted>" in rows[0]["error"]
    assert rows[1] == {"id": "not-an-id", "error": "not a Slack user-id shape"}
    assert [c[0] for c in fake.calls] == ["users_info"]  # the malformed id never reaches Slack
    assert m.exit_code(rows) == 1


def test_probe_main_uses_the_env_token_without_printing_it(monkeypatch, capsys):
    m = _load_probe()
    monkeypatch.setenv("SLACK_BOT_TOKEN", _DUMMY_BOT_TOKEN)
    seen = {}

    def _mk(token):
        seen["token"] = token
        return _FakeClient({"U0B2RM2JYJ1": {"real_name": "Harrison Rogers", "deleted": False}})

    rc = m.main(["--ids", "U0B2RM2JYJ1,U02P3D6AT2C"], make_client=_mk, load_env=False)
    out = capsys.readouterr()
    assert seen["token"] == _DUMMY_BOT_TOKEN
    assert _BOT_PREFIX not in out.out and _BOT_PREFIX not in out.err
    assert rc == 0 and "ANSWER: U0B2RM2JYJ1" in out.out
    assert "U02P3D6AT2C -> ERROR" in out.out and "user_not_found" in out.out


def test_probe_fails_closed_on_an_empty_user_payload():
    """A transport answering ok with no user must never read as 'resolves'."""
    m = _load_probe()
    fake = _FakeClient({"U02P3D6AT2C": {}, "U0B2RM2JYJ1": {"real_name": "Harrison Rogers", "deleted": False}})
    rows = m.probe(m.DEFAULT_IDS, fake)
    assert rows[0] == {"id": "U02P3D6AT2C", "error": "empty user payload"}
    assert m.exit_code(rows) == 0 and "ANSWER: U0B2RM2JYJ1" in m.render(rows)


def test_probe_main_names_the_missing_key_and_exits_2(monkeypatch, capsys):
    m = _load_probe()
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    rc = m.main([], make_client=lambda t: pytest.fail("client built without a token"), load_env=False)
    assert rc == 2
    assert "SLACK_BOT_TOKEN is not set" in capsys.readouterr().err


def test_probe_source_pins_read_only_posture():
    src = _PROBE.read_text(encoding="utf-8")
    assert src.isascii(), "probe script must stay ASCII"
    methods = set(re.findall(r"\bclient\.([a-z_]+)\(", src))
    assert methods == {"users_info"}, methods
    for forbidden in ("chat_", "admin_", "conversations_", "users_profile_set", "reactions_"):
        assert forbidden not in src, forbidden
    # .env is loaded at RUN time inside main, never at import
    assert "load_dotenv(" in src
    body_before_main = src[: src.index("def main(")]
    assert "load_dotenv(" not in body_before_main
    # nothing prints the token
    assert not re.search(r"print\([^)]*token", src)


def test_probe_id_regex_is_linear_on_growth():
    """Growth-shape pin for the new regex (D-239 companion): anchored single class,
    time must stay flat as the input grows 100x."""
    m = _load_probe()
    rx = m._SLACK_USER_ID_RE

    def _t(n: int) -> float:
        s = "U" + "A" * n
        t0 = time.perf_counter()
        for _ in range(200):
            rx.match(s)
        return time.perf_counter() - t0

    small, big = _t(100), _t(10_000)
    assert big < max(small * 50, 0.5), (small, big)
    assert rx.match("U0B2RM2JYJ1") and not rx.match("u0b2rm2jyj1") and not rx.match("C0B6GT3117Y")
