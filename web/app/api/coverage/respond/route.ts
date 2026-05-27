import { NextRequest, NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { createServiceClient } from "@/lib/supabase/server";
import { sendSMS, parentConfirmedSMS } from "@/lib/notifications/twilio";
import { format } from "date-fns";
import { formatTime } from "@/lib/utils";

export async function POST(req: NextRequest) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  const { match_id, action, decline_reason } = await req.json();
  if (!match_id || !["accept", "decline"].includes(action)) {
    return NextResponse.json({ error: "Invalid request" }, { status: 400 });
  }

  const { data: match, error: matchErr } = await supabase
    .from("coverage_matches")
    .select("*, request:coverage_requests(*, member:members(*, parent:profiles(*)))")
    .eq("id", match_id)
    .eq("provider_id", user.id)
    .eq("status", "pending")
    .single();

  if (matchErr || !match) {
    return NextResponse.json({ error: "Match not found or already responded" }, { status: 404 });
  }

  const svcClient = await createServiceClient();

  if (action === "decline") {
    await svcClient.from("coverage_matches").update({
      status:        "declined",
      responded_at:  new Date().toISOString(),
      decline_reason: decline_reason ?? null,
    }).eq("id", match_id);

    // Check if any other pending matches remain for this request
    const { count } = await svcClient
      .from("coverage_matches")
      .select("*", { count: "exact", head: true })
      .eq("request_id", match.request_id)
      .eq("status", "pending");

    if ((count ?? 0) === 0) {
      // No one left — mark request as expired (or re-run matching)
      await svcClient.from("coverage_requests")
        .update({ status: "expired" })
        .eq("id", match.request_id);
    }

    return NextResponse.json({ status: "declined" });
  }

  // Accept: mark this match accepted, expire other pending matches
  await svcClient.from("coverage_matches").update({
    status:       "accepted",
    responded_at: new Date().toISOString(),
  }).eq("id", match_id);

  // Expire other pending matches for this request
  await svcClient.from("coverage_matches").update({ status: "expired" })
    .eq("request_id", match.request_id)
    .eq("status", "pending")
    .neq("id", match_id);

  // Mark availability as booked
  if ((match as any).availability_id) {
    await svcClient.from("provider_availability")
      .update({ is_booked: true })
      .eq("id", (match as any).availability_id);
  }

  // Mark coverage request confirmed
  await svcClient.from("coverage_requests")
    .update({ status: "confirmed" })
    .eq("id", match.request_id);

  // Notify parent via SMS
  const req_ = (match as any).request;
  const parent = req_?.member?.parent;
  const { data: providerProfile } = await svcClient
    .from("profiles").select("full_name").eq("id", user.id).single();

  if (parent?.phone) {
    const smsBody = parentConfirmedSMS({
      parentName:   parent.full_name?.split(" ")[0] ?? "there",
      providerName: providerProfile?.full_name ?? "Your provider",
      date:         req_?.requested_date
        ? format(new Date(req_.requested_date + "T12:00"), "EEEE, MMMM d")
        : "",
      startTime: req_?.start_time ? formatTime(req_.start_time) : "",
      endTime:   req_?.end_time   ? formatTime(req_.end_time)   : "",
    });
    const result = await sendSMS(parent.phone, smsBody);
    await svcClient.from("notifications_log").insert({
      recipient:          parent.phone,
      channel:            "sms",
      message:            smsBody,
      related_request_id: match.request_id,
      success:            result.success,
      error_msg:          result.error ?? null,
    });
  }

  return NextResponse.json({ status: "confirmed" });
}
