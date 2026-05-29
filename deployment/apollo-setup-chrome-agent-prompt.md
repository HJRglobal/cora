# Chrome Agent Prompt — Apollo.io API Key Setup

Paste this entire prompt into a Chrome Agent session. It will walk through the
Apollo.io dashboard, locate the correct API key, confirm plan limits, and report
back everything needed to wire up the F3 LinkedIn Spy scanner.

---

**CHROME AGENT PROMPT:**

You are setting up an Apollo.io API key for the F3 LinkedIn Spy tool — a weekly
scanner that finds retail buyers and executives on LinkedIn and generates
personalized outreach for the F3 Energy sales team. You need to complete Tasks
1–4 below in sequence. Do not move to the next task until the current one is
confirmed complete.

---

**TASK 1: Log in and confirm the Apollo account**

1. Navigate to: https://app.apollo.io
2. Log in if needed (use harrison@hjrglobal.com)
3. Once logged in, confirm you are on the Apollo dashboard (the home screen
   shows a "Sequences", "Contacts", or "Search" navigation on the left)
4. Look at the top-right corner or account menu — note what plan is shown
   (Free, Basic, Professional, Organization, or Trial)
5. Report: confirmed logged in, and the exact plan name shown

---

**TASK 2: Locate and copy the API key**

1. Click your account avatar or initials in the bottom-left corner of the
   left sidebar to open account settings
2. In the settings menu, navigate to: **Integrations** → **API**
   - Direct URL to try: https://app.apollo.io/#/settings/integrations/api
3. On the API page you will see an "API Key" section
4. If a key already exists: click the eye/reveal icon to show the full key,
   then click "Copy" or select and copy the entire key string
5. If no key exists yet: click "Create New Key" or "Generate API Key",
   confirm any prompts, then copy the generated key
6. The key format looks like: a long alphanumeric string (typically 40+ chars)
7. Screenshot the API settings page (mask the middle 20 characters of the
   key for security — show only the first 6 and last 6 characters)
8. Report: the first 6 characters of the key and the last 6 characters
   (so Harrison can confirm it matched what was copied)

---

**TASK 3: Check plan limits and People Search API access**

1. Still on the API settings page (https://app.apollo.io/#/settings/integrations/api)
2. Look for any mention of:
   - **Rate limits** (requests per minute or per hour)
   - **Credit limits** or **export limits** per month
   - **API endpoints available** on this plan
3. Navigate to: https://app.apollo.io/#/settings/billing (or click
   "Billing" / "Plan" in the settings sidebar)
4. On the billing/plan page note:
   - Current plan name
   - Credits remaining this month (look for "Export Credits", "Search Credits",
     or "Email Credits")
   - Plan renewal date or trial expiry date
5. Screenshot the billing/plan page showing the plan name and credit counts
6. Report: plan name, how many search/export credits are available this month,
   and trial expiry date if shown

---

**TASK 4: Test the API key with a quick People Search**

1. Open a new browser tab
2. Open Chrome DevTools (F12) → go to the **Console** tab
3. Paste and run the following JavaScript fetch call (this hits the Apollo
   People Search endpoint to confirm the key works — substitute YOUR_API_KEY
   with the actual key copied in Task 2):

```javascript
fetch("https://api.apollo.io/v1/mixed_people/search", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Cache-Control": "no-cache",
    "X-Api-Key": "YOUR_API_KEY"
  },
  body: JSON.stringify({
    page: 1,
    per_page: 3,
    person_titles: ["buyer", "category manager"],
    organization_industry_tag_ids: [],
    q_keywords: "grocery retail natural foods"
  })
})
.then(r => r.json())
.then(d => console.log("STATUS:", d.status, "| TOTAL RESULTS:", d.pagination?.total_entries, "| FIRST RESULT:", d.people?.[0]?.name, "-", d.people?.[0]?.title, "@", d.people?.[0]?.organization?.name))
.catch(e => console.error("ERROR:", e));
```

4. Wait for the console output (2–5 seconds)
5. Report the exact console output line that starts with "STATUS:" — it will
   show whether the key is valid and how many results the search returned
6. If you see an error like "unauthorized" or "invalid_api_key": go back to
   Task 2, re-copy the key carefully, and retry
7. If you see a CORS or network error in the console: instead open a new tab,
   navigate to https://app.apollo.io, open DevTools console there, and retry
   the fetch from that page (same origin avoids CORS)

---

**TASK 5: Report everything back**

Collect and report ALL of the following:

1. Apollo account email confirmed logged in as
2. Plan name (Free / Trial / Basic / etc.)
3. Trial expiry date (if on trial)
4. Monthly search/export credits available
5. First 6 + last 6 characters of the API key
6. The console output from the Task 4 test (the full STATUS line)
7. Any error messages encountered during any task

**Done when:** API key is confirmed copied, Task 4 test returns STATUS: success
(or equivalent) with a result count greater than 0.

---

**Note for the Chrome Agent:** If Apollo prompts to upgrade the plan before
showing API settings, screenshot the prompt and report it — do not click
"Upgrade" or enter any payment information. Harrison will decide whether to
upgrade. If the free/trial plan does not include API access at all, report
that finding clearly so an alternative data source can be evaluated.
