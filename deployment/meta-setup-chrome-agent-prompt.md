# Chrome Agent Prompt — Meta / Instagram Token Setup

Paste this entire prompt into a Chrome Agent session. It will walk through the
Meta Developer portal and generate the tokens needed for the Cora influencer scanner.

---

**CHROME AGENT PROMPT:**

You are setting up Instagram Graph API credentials for three F3 brand Instagram accounts so the Cora influencer scanner can monitor tagged posts. You need to complete Steps 1–6 from `C:\Users\Harri\code\cora\deployment\META_SETUP_GUIDE.md`. Follow each task in sequence and do not move to the next until the current one is confirmed.

---

**TASK 1: Verify the Meta Developer App exists**

1. Navigate to: https://developers.facebook.com/apps
2. Log in if needed (use Harrison's Facebook account)
3. Look for an existing app named `Cora-HJR` or similar HJR app
4. If it exists: click into it and confirm Instagram Graph API is listed under "Added products" in the left sidebar. Screenshot the app dashboard.
5. If it does NOT exist:
   - Click "Create App"
   - Select "Business" as the use case
   - App name: `Cora-HJR`
   - Contact email: harrison@hjrglobal.com
   - Click through to create
6. Note the **App ID** shown at the top of the dashboard (format: 15-digit number)
7. Go to Settings → Basic → copy the **App Secret** (click "Show")
8. Report: App ID and whether it already had Instagram Graph API added

---

**TASK 2: Confirm Instagram Graph API product is added**

1. In the left sidebar of the app, look for "Instagram Graph API"
2. If not present: click "Add Product" → find "Instagram Graph API" → "Set up"
3. Screenshot the sidebar showing Instagram Graph API is listed
4. Report: confirmed added or just added now

---

**TASK 3: Generate short-lived tokens for all three F3 brand accounts**

Do this three times — once per brand. For each:

1. Navigate to: https://developers.facebook.com/tools/explorer/
2. In the top-right "Meta App" dropdown, select the `Cora-HJR` app
3. Click "Generate Access Token"
4. In the permissions dialog, check ALL four:
   - instagram_basic
   - instagram_manage_insights
   - pages_show_list
   - pages_read_engagement
5. Click "Generate Token" and authorize in the pop-up
6. Copy the token from the "Access Token" field

Run for: F3 Energy (the primary F3 brand account), F3 Mood, F3 Pure.
Note: you may need to select which Facebook Page to authorize for each — pick the matching F3 brand page.

Report all three short-lived tokens (they expire in ~1 hour so move quickly to Task 4).

---

**TASK 4: Exchange each short-lived token for a Long-Lived Token**

For each of the three tokens from Task 3:

1. Open a new browser tab
2. Paste this URL, substituting your values:
   ```
   https://graph.facebook.com/v19.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN
   ```
   Replace APP_ID, APP_SECRET (from Task 1), and SHORT_TOKEN (from Task 3).
3. The page returns JSON. Copy the `access_token` value — this is your Long-Lived Token (~60 days).

Do this for all three accounts.
Report all three Long-Lived Tokens.

---

**TASK 5: Get the numeric IG Business Account User ID for each brand**

For each Long-Lived Token:

1. Open a new browser tab
2. Paste this URL:
   ```
   https://graph.facebook.com/v19.0/me/accounts?fields=name,instagram_business_account&access_token=LONG_LIVED_TOKEN
   ```
   Replace LONG_LIVED_TOKEN with the token for that brand.
3. The response lists Facebook Pages. Find the matching F3 brand page.
4. Copy the value of `instagram_business_account.id` — this is the numeric IG User ID.

Do this for F3 Energy, F3 Mood, and F3 Pure.
Report all three numeric IDs.

---

**TASK 6: Add credentials to the .env file**

1. Open Notepad or VS Code to: `C:\Users\Harri\code\cora\.env`
2. Append these lines at the bottom (fill in the values from Tasks 4 and 5):
   ```
   # Instagram Graph API — F3 Energy (@f3energy)
   INSTAGRAM_F3E_USER_ID=<numeric ID>
   INSTAGRAM_F3E_ACCESS_TOKEN=<long-lived token>

   # Instagram Graph API — F3 Mood (@drinkf3mood)
   INSTAGRAM_F3MOOD_USER_ID=<numeric ID>
   INSTAGRAM_F3MOOD_ACCESS_TOKEN=<long-lived token>

   # Instagram Graph API — F3 Pure (@f3pure)
   INSTAGRAM_F3PURE_USER_ID=<numeric ID>
   INSTAGRAM_F3PURE_ACCESS_TOKEN=<long-lived token>

   # Slack channel for influencer detection alerts (without #)
   INFLUENCER_SCAN_NOTIFY_CHANNEL=f3-sales
   ```
3. Save the file.
4. Screenshot the saved .env showing the new lines (mask the middle of each token for security).

---

**TASK 7: Run a test scan**

1. Open PowerShell
2. Run:
   ```powershell
   cd C:\Users\Harri\code\cora
   uv run python scripts/run_influencer_scan.py
   ```
3. Wait for it to complete (30–60 seconds)
4. Report the last 20 lines of output

**Done when:** All 6 .env values are set, test scan completes without errors.

---
**Note for the Chrome Agent:** If any step hits a permissions error or Meta App Review wall, stop and report the exact error message. Do not attempt to submit an App Review or agree to any Meta business verification — those require Harrison's manual review.
