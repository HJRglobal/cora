"""WS15 — Sara Fonseca (LLC marketing freelancer) name resolution.

She was mapped in slack-to-asana / org-roles / user-permissions but MISSING from
user-aliases.yaml, so "Sara" / "Fonseca" failed to resolve (calendar + task
creation by name). Real-config regression pin.
"""

import pytest

from cora.tools.tool_dispatch import resolve_name_to_slack_user_id


@pytest.mark.parametrize("needle", ["Sara", "Sara Fonseca", "Fonseca", "sara"])
def test_sara_resolves_from_real_config(needle):
    uid, canonical = resolve_name_to_slack_user_id(needle)
    assert uid == "U0B9JS3JW07"
    assert canonical == "Sara Fonseca"
