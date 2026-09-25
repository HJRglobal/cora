"""Code #16 C1 (A30) -- import safety of the dead-channel lane.

The ladder registry's ``channel_archive_mode`` probe imports
``cora.channel_archive.policy`` inside the nightly health check and the Monday digest;
importing it must never build the Bolt app or pull the lane's Slack-facing modules.

VERIFY-FIRST: the design asked that importing policy "not import slack_sdk.web". That
cannot hold for ANY cora module -- ``cora/__init__.py`` installs the egress sanitizer,
which imports ``slack_sdk.web.client`` by design (every process that imports cora gets
the single sanitizing boundary). The pin below asserts the real invariant instead:
no ``cora.app``, no ``slack_bolt``, no other lane module.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "src" / "cora" / "channel_archive"


def _imports(path: Path) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            mods.add("<relative>")
    return mods


def test_the_package_init_and_policy_are_stdlib_only():
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    for name in ("__init__.py", "policy.py"):
        mods = _imports(PKG / name)
        assert mods <= stdlib, (name, mods - stdlib)


def test_importing_policy_pulls_no_bot_or_lane_module():
    code = ("import sys, cora.channel_archive.policy as p; "
            "print(p.acting_tier()); "
            "print('|'.join(sorted(m for m in sys.modules if m.startswith(('cora', 'slack_bolt')))))")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), env.get("PYTHONPATH", "")])
    env.pop("CORA_CHANNEL_ARCHIVE", None)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=120, cwd=str(REPO),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    tier, mods = proc.stdout.strip().splitlines()[-2:]
    loaded = set(mods.split("|"))
    assert tier == "T0"
    assert "cora.app" not in loaded and not [m for m in loaded if m.startswith("slack_bolt")]
    lane = {m for m in loaded if m.startswith("cora.channel_archive.")}
    assert lane == {"cora.channel_archive.policy"}, lane


def test_script_side_modules_never_import_the_bot():
    """The monthly script and the nightly monitor import these; none may import
    cora.app (it builds App(token=...) -> auth.test at import)."""
    for f in PKG.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").endswith("app") or node.module == "slack_bolt.app", f.name
                assert not (node.level and node.module is None
                            and any(a.name == "app" for a in node.names)), f.name
