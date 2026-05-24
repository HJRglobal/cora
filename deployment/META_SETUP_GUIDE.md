# Meta / Instagram Graph API Setup Guide

**Purpose:** Get the Long-Lived User Access Tokens and numeric IG Business Account IDs
needed to run the Cora influencer scanner on the three F3 brand accounts
(@f3energy, @drinkf3mood, @f3pure).

**Time required:** ~30 minutes  
**Who does this:** Harrison (must be Facebook Page admin for all three F3 brand pages)

---

## Prerequisites — confirm before starting

- [ ] All three F3 Instagram accounts (@f3energy, @drinkf3mood, @f3pure) are
      **Instagram Business Accounts** (not Creator or Personal).
      Check: Instagram → Profile → Settings → Account type and tools → Switch account type
- [ ] Each Instagram Business Account is **connected to a Facebook Page** you administer.
      Check: Instagram → Settings → Linked accounts → Facebook
- [ ] You have a Meta Developer account at developers.facebook.com
      (your personal Facebook account works — just needs developer access enabled)

---

## Step 1 — Create (or locate) your Meta App

1. Go to https://developers.facebook.com/apps
2. If you already have an app for Cora/HJR, use it. If not:
   - Click **Create App**
   - Select **Business** as the use case
   - App name: `Cora-HJR` (or similar)
   - App contact email: harrison@hjrglobal.com
   - Business portfolio: select your Meta Business Account if prompted
   - Click **Create app**
3. Note your **App ID** and **App Secret** (Settings → Basic). You won't need
   these in `.env` but keep them handy for the token exchange step.

---

## Step 2 — Add Instagram Graph API to the App

1. From your App Dashboard, click **Add Product** (or find the left sidebar)
2. Find **Instagram Graph API** → click **Set up**
3. You don't need to configure anything here yet — just adding the product
   unlocks the required permissions in the Graph API Explorer

---

## Step 3 — Generate a User Access Token via Graph API Explorer

Do this **once per brand account** (3 times total — F3 Energy, F3 Mood, F3 Pure).
The token must belong to a user who admins the Facebook Page connected to that brand's Instagram.

1. Go to https://developers.facebook.com/tools/explorer/
2. In the top-right **Meta App** dropdown, select `Cora-HJR` (your app)
3. Click **Generate Access Token**
4. In the permissions dialog, check ALL of the following:
   - `instagram_basic`
   - `instagram_manage_insights`
   - `pages_show_list`
   - `pages_read_engagement`
5. Click **Generate Token** → authorize in the pop-up
6. Copy the short-lived token that appears (valid ~1 hour — exchange it immediately in Step 4)

---

## Step 4 — Exchange for a Long-Lived Token (valid 60 days)

Run this in your terminal (substitute your values):

```powershell
# Replace these three values:
$APP_ID     = "YOUR_APP_ID"       # from Step 1 Settings → Basic
$APP_SECRET = "YOUR_APP_SECRET"   # from Step 1 Settings → Basic
$SHORT_TOKEN = "THE_SHORT_LIVED_TOKEN_FROM_STEP_3"

$url = "https://graph.facebook.com/v19.0/oauth/access_token?grant_type=fb_exchange_token&client_id=$APP_ID&client_secret=$APP_SECRET&fb_exchange_token=$SHORT_TOKEN"
Invoke-RestMethod -Uri $url
```

The response will have `access_token` (your Long-Lived Token, ~60 days) and
`expires_in` (seconds). Copy the `access_token` value.

> **Note:** The Cora scanner auto-refreshes tokens when they are within 10 days
> of expiry. You shouldn't need to repeat this step unless the token fully expires.

---

## Step 5 — Get the Numeric IG Business Account User ID

For each brand account, run:

```powershell
$LLAT = "YOUR_LONG_LIVED_TOKEN"  # from Step 4

# List all Facebook Pages + their connected IG accounts
$url = "https://graph.facebook.com/v19.0/me/accounts?fields=name,instagram_business_account&access_token=$LLAT"
Invoke-RestMethod -Uri $url | ConvertTo-Json -Depth 5
```

Look for the entry where `name` matches the F3 brand Facebook Page.
The `instagram_business_account.id` value is the numeric IG User ID you need.

Example output:
```json
{
  "data": [
    {
      "name": "F3 Energy",
      "instagram_business_account": {
        "id": "17841400000000001"
      },
      "id": "123456789"
    }
  ]
}
```

---

## Step 6 — Add credentials to .env

Open `C:\Users\Harri\code\cora\.env` and add:

```
# Instagram Graph API — F3 Energy (@f3energy)
INSTAGRAM_F3E_USER_ID=<numeric ID from Step 5>
INSTAGRAM_F3E_ACCESS_TOKEN=<long-lived token from Step 4>

# Instagram Graph API — F3 Mood (@drinkf3mood)
INSTAGRAM_F3MOOD_USER_ID=<numeric ID from Step 5>
INSTAGRAM_F3MOOD_ACCESS_TOKEN=<long-lived token from Step 4>

# Instagram Graph API — F3 Pure (@f3pure)
INSTAGRAM_F3PURE_USER_ID=<numeric ID from Step 5>
INSTAGRAM_F3PURE_ACCESS_TOKEN=<long-lived token from Step 4>

# Slack channel for influencer detection alerts (without #)
INFLUENCER_SCAN_NOTIFY_CHANNEL=f3-sales
```

---

## Step 7 — Seed athletes and run the first scan

1. Populate athlete handles:
   ```powershell
   cd C:\Users\Harri\code\cora
   # Edit data/seed/athletes.yaml with your athlete roster first
   uv run python scripts/seed_athletes.py
   ```

2. Run the scanner manually to verify:
   ```powershell
   uv run python scripts/run_influencer_scan.py
   ```
   Check `logs/influencer-scan-*.log` for output. You should see it poll
   each brand account and log `scan complete` with detection counts.

3. Register the Windows scheduled task (runs every 2 hours):
   ```powershell
   .\deployment\setup-influencer-scan-task.ps1
   ```

---

## Troubleshooting

| Error | Likely cause | Fix |
|-------|-------------|-----|
| `OAuthException: Invalid OAuth access token` | Token expired or wrong scope | Repeat Steps 3–4 |
| `(#200) Requires instagram_basic permission` | Permissions not granted | Repeat Step 3, ensure all 4 boxes checked |
| `ig_user_id returns empty` | Instagram account not a Business account | Convert to Business Account, re-link to Facebook Page |
| `Hashtag search returns 0 results` | App not approved for hashtag search | Use `/{ig-user-id}/tags` path only; hashtag search requires Meta App Review for some accounts |
| Token not auto-refreshing | Scanner task not running | Check Task Scheduler for `cowork-cora-influencer-scan` |

---

## Token rotation schedule

- Long-Lived Tokens are valid **60 days**
- Cora auto-refreshes when **≤10 days remaining**
- The scanner must run at least once in the final 10-day window for auto-refresh to trigger
- If a token fully expires, repeat Steps 3–4 for that brand account only
