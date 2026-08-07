"""R3 (fan-out Lens B-3.1, HIGH): an #info-for-cora contribution must NEVER
auto-write to known-answers, at any CORA_AUTOWRITE_LIVE level.

Verified chain before the fix: info-for-cora generics are knowledge-class
(knowledge_review.is_knowledge_update), so they entered the 7am drain's auto-write
scan; the live .env carries CORA_AUTOWRITE_LIVE=all; categorize() allowlists
operational/sop/ownership/contacts/logistics/addresses/product_inventory; and the
corroboration verdict comes from a Haiku read whose prompt embeds the
contribution's own text. So an allowlist-category teammate note reading
CORROBORATED would have written itself into always-injected known-answers with no
Harrison tap -- and this branch newly turns ON a previously-dead producer feeding
that drain. Excluding by SOURCE restores D-060.
"""

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

try:
    import scripts.run_knowledge_review as rkr
    _IMPORT_OK = True
except Exception:  # noqa: BLE001
    _IMPORT_OK = False

pytestmark = pytest.mark.skipif(not _IMPORT_OK, reason="review script unavailable")


def _info_for_cora_item():
    """An item that is eligible in EVERY other respect: knowledge-class generic,
    allowlist-shaped operational text, and a CORROBORATED read."""
    return {
        "update_id": "infocora-1",
        "update_type": "generic",
        "description": "#info-for-cora from Tommy (F3E): the Tucson stove vendor is Apex",
        "payload": {
            "text": "The Tucson stove vendor is Apex Appliance",
            "entity": "F3E",
            "source": "info-for-cora",
            "author_id": "U_TOMMY",
        },
        "_coras_read_verdict": "CORROBORATED",
    }


class TestInfoForCoraNeverAutowrites:
    @pytest.mark.parametrize("level", ["tier0", "all"])
    def test_excluded_at_every_level(self, level):
        eligible, _tier, why = rkr._autowrite_eligible(_info_for_cora_item(), level)
        assert eligible is False
        assert why == "info_for_cora_never_autowrites"

    def test_exclusion_is_keyed_on_source_not_entity_or_text(self):
        """Same item without the info-for-cora source is NOT excluded by this
        predicate -- proving the exclusion is the thing doing the work here and the
        test would fail if the predicate were deleted."""
        item = _info_for_cora_item()
        item["payload"]["source"] = "slack"
        _eligible, _tier, why = rkr._autowrite_eligible(item, "all")
        assert why != "info_for_cora_never_autowrites"

    def test_missing_payload_does_not_crash_the_predicate(self):
        eligible, _tier, _why = rkr._autowrite_eligible(
            {"update_id": "x", "update_type": "generic"}, "all")
        assert eligible in (True, False)
