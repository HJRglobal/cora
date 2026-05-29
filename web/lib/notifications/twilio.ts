// Twilio SMS client — used server-side only (API routes / Server Actions)
// Never import this in client components.

const ACCOUNT_SID  = process.env.TWILIO_ACCOUNT_SID!;
const AUTH_TOKEN   = process.env.TWILIO_AUTH_TOKEN!;
const FROM_NUMBER  = process.env.TWILIO_PHONE_NUMBER!;

interface SendResult {
  success: boolean;
  sid?: string;
  error?: string;
}

export async function sendSMS(to: string, body: string): Promise<SendResult> {
  if (!ACCOUNT_SID || !AUTH_TOKEN || !FROM_NUMBER) {
    console.error("[twilio] Missing env vars — SMS not sent");
    return { success: false, error: "Twilio not configured" };
  }

  const url = `https://api.twilio.com/2010-04-01/Accounts/${ACCOUNT_SID}/Messages.json`;
  const params = new URLSearchParams({ To: to, From: FROM_NUMBER, Body: body });

  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: "Basic " + btoa(`${ACCOUNT_SID}:${AUTH_TOKEN}`),
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: params.toString(),
    });

    if (!res.ok) {
      const err = await res.text();
      return { success: false, error: err };
    }

    const data = await res.json();
    return { success: true, sid: data.sid };
  } catch (e) {
    return { success: false, error: String(e) };
  }
}

// ─── Notification templates ───────────────────────────────────

export function providerMatchSMS(opts: {
  providerName: string;
  memberFirstName: string;
  serviceLabel: string;
  date: string;
  startTime: string;
  endTime: string;
  acceptUrl: string;
}): string {
  return (
    `Hi ${opts.providerName} — Lexington has a coverage request for you!\n\n` +
    `Member: ${opts.memberFirstName}\n` +
    `Service: ${opts.serviceLabel}\n` +
    `Date: ${opts.date}\n` +
    `Time: ${opts.startTime} – ${opts.endTime}\n\n` +
    `Tap to accept or decline:\n${opts.acceptUrl}`
  );
}

export function parentMatchFoundSMS(opts: {
  parentName: string;
  providerName: string;
  date: string;
  startTime: string;
}): string {
  return (
    `Hi ${opts.parentName} — great news! A provider has been matched for your coverage request.\n\n` +
    `Provider: ${opts.providerName}\n` +
    `Date: ${opts.date} at ${opts.startTime}\n\n` +
    `Check your Lexington portal for details: ${process.env.NEXT_PUBLIC_APP_URL}`
  );
}

export function parentConfirmedSMS(opts: {
  parentName: string;
  providerName: string;
  date: string;
  startTime: string;
  endTime: string;
}): string {
  return (
    `Coverage confirmed! ✓\n\n` +
    `${opts.providerName} will provide services for your family member.\n` +
    `Date: ${opts.date}\n` +
    `Time: ${opts.startTime} – ${opts.endTime}\n\n` +
    `Questions? Contact Lexington Services at your usual number.`
  );
}

export function requestExpiredSMS(parentName: string): string {
  return (
    `Hi ${parentName} — we were unable to find an available provider for your ` +
    `coverage request. Please call Lexington Services directly so we can assist you. ` +
    `We apologize for the inconvenience.`
  );
}
