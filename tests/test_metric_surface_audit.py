"""Never-lit metric audit (session #11 S5, the D-215 class).

THE CLASS. A threshold that reads a metric nothing writes can never fire; a metric
written but read by nothing is collected for no one. Both are silent -- a green
suite and a clean health check look identical either way. D-206's MECHANICAL
BACKLOG alarm sat in the first state ("it was never dark -- it was never lit").

WHY THIS IS A TEST AND NOT A RUNTIME LANE. The join is a static property of the
module. A runtime checker would need live data and would itself be a thing that
can go dark.

THE SPEC'S SECOND RULE IS DELIBERATELY NOT IMPLEMENTED AS WRITTEN. The slice asked
for a symmetric pair -- "a threshold with no writer FAILS, a writer with no
threshold FAILS". Implemented literally the second half fails on ~19 legitimate
display-only gauges that format_lines() renders for a human and no threshold
guards. Implemented with an allowlist that large it asserts nothing. The invariant
with teeth is: every collected metric must reach a HUMAN SURFACE -- evaluate() OR
format_lines(). That yields a small, meaningful dark set.

THE ENUMERATOR IS THE RISK SURFACE. Metric keys are written in four idioms and read
in three, and a walker that misses one silently under-reports. While building this
I wrote a naive version that missed `out.update(k=v)` keyword form and reported
three false "thresholds with no writer"; a second version missed the
`"k" not in metrics` read form. An enumerator that returns nothing would make every
assertion below pass vacuously -- exactly the "a bar that cannot fail on the
regression it guards is not a bar" defect this session exists to retire. So the
enumerator is itself pinned by TestEnumeratorCannotPassVacuously.
"""
from __future__ import annotations

import ast
import io
from pathlib import Path

_MODULE = Path("src/cora/flywheel_metrics.py")

# Metrics collect() writes that reach NEITHER evaluate() nor format_lines().
# Each needs a one-line justification. A new dark key fails the suite -- that is
# the point. Fix by surfacing it, or declare it here and say why.
_DECLARED_DARK: dict[str, str] = {
    "baseline_days": "internal denominator for the t0 baseline window; not a "
                     "reader-facing figure on its own",
    "ledger_error": "diagnostic breadcrumb set when the ledger scan raises; the "
                    "affected gauges degrade to None and format_lines renders "
                    "their absence. NOTE: surfacing this is the highest-value "
                    "follow-on this slice identified -- today a ledger-scan "
                    "failure is indistinguishable from a genuinely quiet week",
    "wave1_conversion_error": "same shape as ledger_error, for the wave-1 "
                              "conversion block",
}


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(io.open(_MODULE, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("function %s() not found in %s" % (name, _MODULE))


def _keys_written(fn: ast.FunctionDef) -> set[str]:
    """Four idioms, all present in collect(). Missing any one under-reports."""
    keys: set[str] = set()
    for n in ast.walk(fn):
        # 1. out["k"] = ...
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (
                    isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name)
                    and isinstance(t.slice, ast.Constant)
                    and isinstance(t.slice.value, str)
                ):
                    keys.add(t.slice.value)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "update":
            # 2. out.update({...})
            for a in n.args:
                if isinstance(a, ast.Dict):
                    for k in a.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
            # 3. out.update(k=v) -- keyword form; a naive walker misses this entirely
            for kw in n.keywords:
                if kw.arg:
                    keys.add(kw.arg)
        # 4. the dict INITIALIZER: out = {"available": True}
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            tgt = n.targets[0] if isinstance(n, ast.Assign) else n.target
            if isinstance(tgt, ast.Name) and tgt.id == "out" and isinstance(n.value, ast.Dict):
                for k in n.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
    return keys


def _keys_read(fn: ast.FunctionDef) -> set[str]:
    """Three idioms: .get("k"), metrics["k"], and "k" in/not in metrics."""
    keys: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get":
            if n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                keys.add(n.args[0].value)
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
           and isinstance(n.slice.value, str):
            keys.add(n.slice.value)
        # "k" in metrics / "k" not in metrics -- how evaluate() tests gaps_error
        if isinstance(n, ast.Compare):
            for op in n.ops:
                if isinstance(op, (ast.In, ast.NotIn)) and isinstance(n.left, ast.Constant) \
                   and isinstance(n.left.value, str):
                    keys.add(n.left.value)
    return keys


class TestEnumeratorCannotPassVacuously:
    """If the enumerator silently returns empty, every assertion below passes and
    the rail is decorative. These pin that it actually reads the module."""

    def test_writer_enumeration_is_substantial(self):
        assert len(_keys_written(_fn("collect"))) >= 20

    def test_reader_enumerations_are_substantial(self):
        assert len(_keys_read(_fn("evaluate"))) >= 5
        assert len(_keys_read(_fn("format_lines"))) >= 20

    def test_known_keys_are_found_in_each_idiom(self):
        """One representative key per write idiom actually present in collect()."""
        written = _keys_written(_fn("collect"))
        for key in (
            "available",              # dict initializer
            "gaps_error",             # out["k"] = ...
            "mechanical_pending",     # out.update(k=v) keyword form
            "knowledge_dms_7d",       # out.update(k=v) keyword form
        ):
            assert key in written, "enumerator missed %r -- it is under-reporting" % key

    def test_in_comparison_read_form_is_detected(self):
        """evaluate() tests gaps_error with `"gaps_error" not in metrics`."""
        assert "gaps_error" in _keys_read(_fn("evaluate"))


class TestMetricSurfaceJoin:
    def test_no_threshold_reads_a_metric_nothing_writes(self):
        """A threshold keyed on a metric collect() never sets can NEVER fire.
        This is the D-206 shape."""
        written = _keys_written(_fn("collect"))
        read = _keys_read(_fn("evaluate"))
        orphan = sorted(read - written)
        assert not orphan, (
            "evaluate() reads metric(s) collect() never writes -- these thresholds "
            "can never fire: %s" % orphan
        )

    def test_every_collected_metric_reaches_a_human_surface(self):
        """The corrected invariant: evaluate() OR format_lines(), or declared dark."""
        written = _keys_written(_fn("collect"))
        surfaced = _keys_read(_fn("evaluate")) | _keys_read(_fn("format_lines"))
        dark = sorted(written - surfaced - set(_DECLARED_DARK))
        assert not dark, (
            "metric(s) collected but reaching no human surface -- surface them in "
            "evaluate()/format_lines(), or add to _DECLARED_DARK with a reason: %s" % dark
        )

    def test_declared_dark_list_has_no_stale_entries(self):
        """A declared key that is now surfaced (or gone) is drift."""
        written = _keys_written(_fn("collect"))
        surfaced = _keys_read(_fn("evaluate")) | _keys_read(_fn("format_lines"))
        stale = sorted(k for k in _DECLARED_DARK if k not in written or k in surfaced)
        assert not stale, "declared dark but now surfaced or removed: %s" % stale

    def test_static_join_does_not_imply_runtime_liveness(self):
        """Documentation-as-test. collect() is failure-tolerant by design: every
        section is wrapped and degrades to absent/None/0, so a key being ASSIGNED
        somewhere says nothing about whether it is PRESENT on a given run. This
        rail proves wiring, not liveness -- do not read a green here as proof that
        an alarm has ever actually fired."""
        assert set(_DECLARED_DARK) <= _keys_written(_fn("collect"))
