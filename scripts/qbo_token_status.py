"""Quick QBO token status report."""
import json, time, datetime

with open(".credentials/qbo-tokens.json") as f:
    data = json.load(f)

now = time.time()
fmt = "%Y-%m-%d"
print(f"Now: {datetime.datetime.utcfromtimestamp(now).strftime(fmt + ' %H:%M UTC')}\n")
print(f"{'Entity':<12} {'Last Refreshed':<16} {'Refresh Token Expired':<22} {'Days Since Refresh'}")
print("-" * 75)
for entity, tok in sorted(data.items()):
    rt_exp = tok.get("refresh_token_expires_at", 0)
    last = tok.get("last_refreshed_at", 0)
    last_str = datetime.datetime.utcfromtimestamp(last).strftime(fmt) if last else "never"
    days_since = int((now - last) / 86400) if last else 999
    if rt_exp < now:
        days_expired = int((now - rt_exp) / 86400)
        status = f"EXPIRED {days_expired}d ago"
    else:
        days_remaining = int((rt_exp - now) / 86400)
        status = f"valid ({days_remaining}d left)"
    print(f"{entity:<12} {last_str:<16} {status:<22} {days_since}d")
