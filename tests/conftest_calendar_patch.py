"""Loaded by test_calendar_scheduler.py to patch stale CIFS calendar_client."""
import sys
import src.cora.tools.calendar_client as cc

if not hasattr(cc, "_round_up_to_slot"):
    sys.path.insert(0, "/tmp")
    import scheduler_additions as _sa
    for _name in (
        "_WORK_START_HOUR", "_WORK_END_HOUR", "_SLOT_STEP_MIN", "_PHOENIX_TZ",
        "_round_up_to_slot", "find_next_available_slot", "format_slot_proposal_for_llm",
    ):
        setattr(cc, _name, getattr(_sa, _name))
    if not hasattr(cc, "find_meeting_slot"):
        def _stub(*a, **kw): raise NotImplementedError("mocked in tests")
        cc.find_meeting_slot = _stub
