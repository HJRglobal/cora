# Chrome Agent Prompt -- Meta / Instagram Token Setup
# For Cora F3 Fighter Influencer Tracking
#
# Paste everything between the dashed lines into a Claude in Chrome session.
# Harrison must already be logged into his Facebook/Meta account in that browser.

---

You are setting up Instagram Graph API credentials so Cora can monitor when
F3 Energy's 57 sponsored fighters post to Instagram. You need one Long-Lived
Access Token and one numeric IG User ID -- both for the @f3energy account.

The user (Harrison) is already logged into his Facebook/Meta account in this
browser. Work through each task in order. Do not skip ahead. Report clearly
after each task before moving to the next.

---

TASK 1 -- Find or create the Meta Developer App

1. Navigate to: https://developers.facebook.com/apps
2. Look for an existing app named "Cora-HJR" or any HJR app you recognize.
3. If found: click into it. Note the App ID shown at the top (15-digit number).
   Go to Settings > Basic > click "Show" next to App Secret and copy it.
4. If NOT found: click "Create App", select "Business" as use case,
   name it "Cora-HJR", email harrison@hjrglobal.com, click through to create.
   Then go to Settings > Basic and copy both App ID and App Secret.
5. Still in the app: look at the left sidebar for "Instagram Graph API".
   If it is not listed, click "Add Product", find "Instagram Graph API",
   and click Set Up.

REPORT: App ID, whether it was existing or new, whether Instagram Graph API
was already added or just added.

---

TASK 2 -- Generate a short-lived User Access Token

1. Navigate to: https://developers.facebook.com/tools/explorer/
2. In the top-right "Meta App" dropdown, select your Cora-HJR app.
3. Click "Generate Access Token".
4. A permissions dialog appears. Select ALL of these (expand sections as needed):
   - instagram_basic
   - instagram_manage_insights
   - pages_show_list
   - pages_read_engagement
5. Click "Generate Token". A pop-up will ask you to authorize -- approve it.
   If it asks which Facebook Page to connect, select the F3 Energy page.
6. The Access Token field now shows a long string starting with "EAA...".
   Copy the entire token.

REPORT: The short-lived token (it expires in about 1 hour, so move fast).
Also report which Facebook Page was selected during authorization.

---

TASK 3 -- Exchange for a Long-Lived Token (valid 60 days)

You will construct a URL and navigate to it. Replace the three placeholders
with the real values from Tasks 1 and 2:

URL pattern:
https://graph.facebook.com/v19.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN

1. Build that URL with your App ID, App Secret, and the short-lived token.
2. Navigate to it in a new tab. You will see raw JSON on the page.
3. Copy the value of "access_token" from the JSON. This is the Long-Lived Token.
   It will be a much longer string, also starting with "EAA...".
4. Also note the "expires_in" value (should be around 5184000, which is 60 days).

REPORT: The Long-Lived Token and the expires_in value.

---

TASK 4 -- Get the numeric IG Business Account ID for @f3energy

Note: We may already have this (17841448560031091) but verify it is correct.

1. Build this URL using your Long-Lived Token:
   https://graph.facebook.com/v19.0/me/accounts?fields=name,instagram_business_account&access_token=LONG_LIVED_TOKEN
2. Navigate to it. You will see a JSON list of Facebook Pages you admin.
3. Find the entry where "name" contains "F3 Energy".
4. Copy the value of instagram_business_account.id for that entry.
   It will be a 17-digit number.

REPORT: The numeric IG Business Account ID for F3 Energy, and whether it
matches 17841448560031091.

---

TASK 5 -- Add credentials to the .env file

1. Open this file in Notepad or VS Code:
   C:\Users\Harri\code\cora\.env
2. Find any existing lines that start with INSTAGRAM_F3E_ and update them,
   or add these lines if they do not exist:

   INSTAGRAM_F3E_USER_ID=<17-digit number from Task 4>
   INSTAGRAM_F3E_ACCESS_TOKEN=<long-lived token from Task 3>

3. Save the file.

REPORT: Confirm the lines are saved. Show the first 10 characters and last
5 characters of the token so we can verify without exposing the full value.

---

TASK 6 -- Run a test scan and confirm it works

1. Open PowerShell.
2. Run these commands:

   cd C:\Users\Harri\code\cora
   .venv\Scripts\python.exe scripts\run_influencer_scan.py

3. Wait for it to finish (30 to 60 seconds).
4. Read the last 30 lines of the log file:

   Get-Content logs\influencer-scan-*.log | Select-Object -Last 30

REPORT: Paste the last 30 lines of log output. A successful run will show
lines containing "scan complete" for each brand account. Zero detections
is fine and expected -- it means the connection worked but no new posts
were found since the last scan time (watermark).

If you see "OAuthException" or "Invalid OAuth access token", stop and
report the exact error -- do not retry.

---

TASK 7 -- Re-register the scan task with the new twice-daily schedule

The scan previously ran every 2 hours. We are changing it to 7 AM and 7 PM.

1. In PowerShell (still in C:\Users\Harri\code\cora), run:

   .\deployment\setup-influencer-scan-task.ps1

2. Confirm the output shows:
   Schedule : Daily at 7:00 AM and 7:00 PM

REPORT: Paste the output of the setup script.

---

DONE when: Task 6 log shows "scan complete" without auth errors, and
Task 7 shows the new 7 AM / 7 PM schedule confirmed.

If Meta requires App Review or business verification at any point during
Tasks 1-3, STOP immediately and report the exact message shown. Do not
submit any App Review or agree to any business verification -- those require
Harrison to review manually before proceeding.
