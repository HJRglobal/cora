"""Unit tests for supervisor-hierarchy authorization (Cora third-party Asana lookups).

Rule (Harrison 2026-05-21): only direct or transitive supervisors of a person
can query that person's Asana tasks via asana_get_user_tasks. Harrison (founder)
is universal override. Self-query always allowed.
"""

from textwrap import dedent

import cora.tools.tool_dispatch as td


# Slack user IDs used throughout the tests (mirror the real slack-to-asana.yaml shape).
HARRISON = "U0B2RM2JYJ1"
HANNAH = "U0B3AEQS0NB"
MICAH = "U0B4L78SZHN"
DEMI = "U0B3RU65TFU"
LARRY = "U0B3NGR1Y85"
DANIEL = "U0B3PS63F1C"
JAKE = "U0B3AER7NUF"
SHAUN = "U0B3PS82G30"
JEN = "U0B3VGT8RE0"
JUSTIN = "U0B3AEJCYGP"
ERIC = "U0B3PRZMBCN"
TOMMY = "U0B3RU5Q55G"


def _write_hierarchy(tmp_path, monkeypatch):
    """Write a realistic supervisor-hierarchy.yaml under tmp_path and point the
    module constant at it."""
    yaml_text = dedent(f"""
        founder_slack_id: {HARRISON}
        reports_to:
          - report: {HANNAH}
            supervisor: {HARRISON}
          - report: {MICAH}
            supervisor: {HARRISON}
          - report: {SHAUN}
            supervisor: {HARRISON}
          - report: {JUSTIN}
            supervisor: {HARRISON}
          - report: {TOMMY}
            supervisor: {HARRISON}
          - report: {DEMI}
            supervisor: {MICAH}
          - report: {LARRY}
            supervisor: {DEMI}
          - report: {DANIEL}
            supervisor: {DEMI}
          - report: {JAKE}
            supervisor: {DANIEL}
          - report: {JEN}
            supervisor: {SHAUN}
          - report: {ERIC}
            supervisor: {JUSTIN}
    """).strip()
    yaml_file = tmp_path / "supervisor-hierarchy.yaml"
    yaml_file.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(td, "_HIERARCHY_PATH", yaml_file)


# --- Founder override ---


def test_harrison_can_query_anyone(monkeypatch, tmp_path):
    _write_hierarchy(tmp_path, monkeypatch)
    for target in [HANNAH, MICAH, DEMI, LARRY, DANIEL, JAKE, SHAUN, JEN, JUSTIN, ERIC, TOMMY]:
        ok, _ = td.is_authorized_to_query_user(HARRISON, target)
        assert ok is True, f"Harrison should be authorized to query {target}"


# --- Direct supervisor allowed ---


def test_direct_supervisor_can_query_report(monkeypatch, tmp_path):
    _write_hierarchy(tmp_path, monkeypatch)
    # Shaun -> Jen (direct report)
    ok, msg = td.is_authorized_to_query_user(SHAUN, JEN)
    assert ok is True
    assert msg is None

    # Larry's chain: Larry -> Demi -> Micah -> Harrison. Larry has no reports
    # (in this simplified hierarchy). Daniel -> Jake confirms direct chain works.
    ok, _ = td.is_authorized_to_query_user(DANIEL, JAKE)
    assert ok is True

    # Justin -> Eric (direct report)
    ok, _ = td.is_authorized_to_query_user(JUSTIN, ERIC)
    assert ok is True


# --- Transitive supervisor allowed ---


def test_transitive_supervisor_can_query_grand_report(monkeypatch, tmp_path):
    _write_hierarchy(tmp_path, monkeypatch)
    # Micah supervises Demi who supervises Larry/Daniel. Micah should be able
    # to query both.
    ok, _ = td.is_authorized_to_query_user(MICAH, LARRY)
    assert ok is True

    ok, _ = td.is_authorized_to_query_user(MICAH, DANIEL)
    assert ok is True

    # Demi can query Jake (Demi -> Daniel -> Jake)
    ok, _ = td.is_authorized_to_query_user(DEMI, JAKE)
    assert ok is True

    # Harrison can query Jake (Harrison -> Micah -> Demi -> Daniel -> Jake)
    ok, _ = td.is_authorized_to_query_user(HARRISON, JAKE)
    assert ok is True


# --- Peer query denied ---


def test_peers_cannot_query_each_other(monkeypatch, tmp_path):
    _write_hierarchy(tmp_path, monkeypatch)
    # Larry and Daniel both report to Demi — peers, no supervisory relationship.
    ok, msg = td.is_authorized_to_query_user(LARRY, DANIEL)
    assert ok is False
    assert msg is not None
    assert "Not authorized" in msg

    ok, msg = td.is_authorized_to_query_user(DANIEL, LARRY)
    assert ok is False

    # Hannah and Larry — Hannah reports to Harrison, Larry reports to Demi.
    # Different chains. Peer-equivalent at the Harrison-distance-3 vs distance-1 level.
    ok, _ = td.is_authorized_to_query_user(HANNAH, LARRY)
    assert ok is False


# --- Upward query denied ---


def test_report_cannot_query_supervisor(monkeypatch, tmp_path):
    _write_hierarchy(tmp_path, monkeypatch)
    # Jen reports to Shaun — Jen cannot query Shaun's tasks.
    ok, _ = td.is_authorized_to_query_user(JEN, SHAUN)
    assert ok is False

    # Larry reports to Demi — Larry cannot query Demi's tasks.
    ok, _ = td.is_authorized_to_query_user(LARRY, DEMI)
    assert ok is False

    # Jake -> Daniel — Jake cannot query Daniel.
    ok, _ = td.is_authorized_to_query_user(JAKE, DANIEL)
    assert ok is False


# --- Self-query allowed ---


def test_self_query_allowed(monkeypatch, tmp_path):
    _write_hierarchy(tmp_path, monkeypatch)
    # While the canonical path uses asana_get_my_tasks, the authorization
    # function itself should not refuse a self-query.
    ok, _ = td.is_authorized_to_query_user(TOMMY, TOMMY)
    assert ok is True


# --- Cross-functional manager denied ---


def test_cross_functional_managers_cannot_cross_query(monkeypatch, tmp_path):
    _write_hierarchy(tmp_path, monkeypatch)
    # Shaun supervises Jen (Lex). Larry supervises BDM team. Neither can query
    # the other's reports.
    ok, _ = td.is_authorized_to_query_user(SHAUN, JAKE)
    assert ok is False

    ok, _ = td.is_authorized_to_query_user(LARRY, JEN)
    assert ok is False

    # Justin (finance) cannot query Lex team
    ok, _ = td.is_authorized_to_query_user(JUSTIN, JEN)
    assert ok is False


# --- Supervisor chain reflection ---


def test_supervisor_chain_for_jake_is_full_path_to_founder(monkeypatch, tmp_path):
    _write_hierarchy(tmp_path, monkeypatch)
    chain = td._get_supervisor_chain(JAKE)
    # Jake -> Daniel -> Demi -> Micah -> Harrison
    assert chain == [DANIEL, DEMI, MICAH, HARRISON]


def test_supervisor_chain_for_unknown_user_is_empty(monkeypatch, tmp_path):
    _write_hierarchy(tmp_path, monkeypatch)
    chain = td._get_supervisor_chain("U_NEVER_HEARD_OF")
    assert chain == []


# --- Missing-file graceful degradation ---


def test_missing_hierarchy_file_denies_non_founder_queries(monkeypatch, tmp_path):
    # Point at a file that doesn't exist
    monkeypatch.setattr(td, "_HIERARCHY_PATH", tmp_path / "nonexistent.yaml")
    # Without founder configured, even Harrison can't query others (defense-in-depth refuse).
    ok, _ = td.is_authorized_to_query_user(HARRISON, JEN)
    assert ok is False
    # Self-query still works (doesn't need hierarchy)
    ok, _ = td.is_authorized_to_query_user(HARRISON, HARRISON)
    assert ok is True
