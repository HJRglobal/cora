// Coverage matching engine — runs server-side via API route / Server Action
// Finds the best available providers for a coverage request and creates match records.

import { createServiceClient } from "@/lib/supabase/server";
import type { CoverageRequest, ProviderAvailability, Provider, Profile } from "@/lib/supabase/types";
import { sendSMS, providerMatchSMS } from "@/lib/notifications/twilio";
import { format } from "date-fns";
import { SERVICE_LABELS } from "@/lib/supabase/types";

const APP_URL = process.env.NEXT_PUBLIC_APP_URL ?? "https://coverage.lexingtonservices.com";
const MAX_PROVIDERS_TO_NOTIFY = 3; // notify top 3, first to accept wins

export interface MatchResult {
  matched: number;
  providerIds: string[];
  error?: string;
}

export async function runMatchingEngine(requestId: string): Promise<MatchResult> {
  const supabase = await createServiceClient();

  // 1. Load the request
  const { data: request, error: reqErr } = await supabase
    .from("coverage_requests")
    .select("*, member:members(*)")
    .eq("id", requestId)
    .single();

  if (reqErr || !request) {
    return { matched: 0, providerIds: [], error: "Request not found" };
  }

  const req = request as CoverageRequest & { member: { permanent_provider_id: string | null } };

  // 2. Find available providers for the date/time/service
  //    Exclude the permanent provider (they're absent) and already-booked slots
  const { data: slots, error: slotErr } = await supabase
    .from("provider_availability")
    .select("*, provider:providers(*, profile:profiles(*))")
    .eq("date", req.requested_date)
    .eq("service_code", req.service_code)
    .eq("is_booked", false)
    .lte("start_time", req.start_time)   // slot starts at or before requested start
    .gte("end_time", req.end_time)        // slot ends at or after requested end
    .neq("provider_id", req.member?.permanent_provider_id ?? "none");

  if (slotErr || !slots || slots.length === 0) {
    // No available providers — mark request as expired after timeout (handled by cron)
    return { matched: 0, providerIds: [] };
  }

  // 3. Score and rank providers (simple scoring — extend as needed)
  const scored = (slots as Array<ProviderAvailability & {
    provider: Provider & { profile: Profile }
  }>).map((slot) => ({
    slot,
    score: scoreProvider(slot),
  })).sort((a, b) => b.score - a.score);

  const top = scored.slice(0, MAX_PROVIDERS_TO_NOTIFY);

  // 4. Create match records and send SMS notifications
  const providerIds: string[] = [];

  for (const { slot } of top) {
    const { error: matchErr } = await supabase.from("coverage_matches").insert({
      request_id:      requestId,
      provider_id:     slot.provider_id,
      availability_id: slot.id,
      status:          "pending",
      notified_at:     new Date().toISOString(),
    });

    if (matchErr) continue;

    providerIds.push(slot.provider_id);

    // Send SMS alert to provider
    const acceptUrl = `${APP_URL}/provider/respond/${requestId}`;
    const message = providerMatchSMS({
      providerName:   slot.provider.profile.full_name.split(" ")[0],
      memberFirstName: "a member",  // first name only, no PHI in SMS
      serviceLabel:   SERVICE_LABELS[req.service_code],
      date:           format(new Date(req.requested_date), "EEEE, MMM d"),
      startTime:      formatTime(req.start_time),
      endTime:        formatTime(req.end_time),
      acceptUrl,
    });

    const smsResult = await sendSMS(slot.provider.profile.phone, message);

    // Log the notification
    await supabase.from("notifications_log").insert({
      recipient:          slot.provider.profile.phone,
      channel:            "sms",
      message,
      related_request_id: requestId,
      success:            smsResult.success,
      error_msg:          smsResult.error ?? null,
    });
  }

  // 5. Update request status to "matched" if we found anyone
  if (providerIds.length > 0) {
    await supabase
      .from("coverage_requests")
      .update({ status: "matched" })
      .eq("id", requestId);
  }

  return { matched: providerIds.length, providerIds };
}

function scoreProvider(slot: ProviderAvailability & { provider: Provider & { profile: Profile } }): number {
  let score = 0;
  // Cleared background check is required
  if (!slot.provider.background_check_cleared) return -1;
  // Credential not expired
  if (slot.provider.credential_expiry) {
    const expiry = new Date(slot.provider.credential_expiry);
    if (expiry < new Date()) return -1;
    // Bonus for credentials expiring further out
    const daysRemaining = (expiry.getTime() - Date.now()) / 86400000;
    score += Math.min(daysRemaining / 30, 10); // up to 10 pts
  }
  // More available hours = better
  score += Math.min(slot.provider.max_weekly_hours / 10, 4);
  return score;
}

function formatTime(t: string): string {
  // "14:00:00" → "2:00 PM"
  const [h, m] = t.split(":").map(Number);
  const ampm = h >= 12 ? "PM" : "AM";
  const hour = h % 12 || 12;
  return `${hour}:${String(m).padStart(2, "0")} ${ampm}`;
}
