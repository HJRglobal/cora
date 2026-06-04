"""Live QBO smoke test -- HJRP entity + per-property class filtering."""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from cora.tools.qbo_client import (
    get_profit_loss, get_balance_sheet, get_recent_transactions,
    format_pnl_for_llm, format_balance_sheet_for_llm,
    format_recent_transactions_for_llm,
    _HJRP_CLASS_MAP,
)

print("=" * 60)
print("QBO SMOKE TEST -- HJRP")
print("=" * 60)

# 1. HJRP-level P&L (no class filter)
print("\n[1] HJRP entity-level P&L (YTD)")
try:
    r = get_profit_loss("HJRP", "2026-01-01", "2026-06-04")
    print(format_pnl_for_llm(r, "HJRP", "2026-01-01", "2026-06-04"))
except Exception as e:
    print(f"  ERROR: {e}")

# 2. North Hampton (1337) class-filtered P&L
print("\n[2] HJRP-1337 North Hampton P&L (YTD, class=7005-105)")
try:
    r = get_profit_loss("HJRP-1337", "2026-01-01", "2026-06-04")
    print(format_pnl_for_llm(r, "HJRP-1337", "2026-01-01", "2026-06-04"))
except Exception as e:
    print(f"  ERROR: {e}")

# 3. South Hampton (1555) class-filtered P&L
print("\n[3] HJRP-1555 South Hampton P&L (YTD, class=9400-703)")
try:
    r = get_profit_loss("HJRP-1555", "2026-01-01", "2026-06-04")
    print(format_pnl_for_llm(r, "HJRP-1555", "2026-01-01", "2026-06-04"))
except Exception as e:
    print(f"  ERROR: {e}")

# 4. Balance sheet
print("\n[4] HJRP Balance Sheet (today)")
try:
    r = get_balance_sheet("HJRP")
    print(format_balance_sheet_for_llm(r, "HJRP", "2026-06-04"))
except Exception as e:
    print(f"  ERROR: {e}")

# 5. Recent transactions
print("\n[5] HJRP Recent Transactions (last 30d)")
try:
    r = get_recent_transactions("HJRP", days=30)
    print(format_recent_transactions_for_llm(r, "HJRP", 30))
except Exception as e:
    print(f"  ERROR: {e}")

# 6. Discover all QBO classes in this company -- find Rogers Ranch
print("\n[6] QBO CLASS DISCOVERY -- all active classes in HJRP company")
try:
    from cora.connectors.qbo_oauth import get_valid_access_token
    from cora.tools.qbo_client import _api_base_for_entity, _realm_id
    import httpx
    access_token, realm_id = get_valid_access_token("HJRP")
    base = _api_base_for_entity("HJRP")
    url = f"{base}/v3/company/{realm_id}/query"
    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": "SELECT * FROM Class WHERE Active = true MAXRESULTS 100", "minorversion": "65"},
        timeout=30,
    )
    if resp.status_code == 200:
        classes = resp.json().get("QueryResponse", {}).get("Class", [])
        print(f"  Found {len(classes)} active classes:")
        for c in sorted(classes, key=lambda x: x.get("FullyQualifiedName", "")):
            fqn = c.get("FullyQualifiedName", c.get("Name", "?"))
            cid = c.get("Id", "?")
            print(f"    Id={cid:<8}  {fqn}")
    else:
        print(f"  HTTP {resp.status_code}: {resp.text[:300]}")
except Exception as e:
    print(f"  ERROR: {e}")

print("\n" + "=" * 60)
print("SMOKE TEST COMPLETE")
print("=" * 60)
