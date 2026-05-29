import { NextRequest, NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { runMatchingEngine } from "@/lib/matching/engine";

export async function POST(req: NextRequest) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // Verify caller is a parent
  const { data: profile } = await supabase
    .from("profiles")
    .select("role")
    .eq("id", user.id)
    .single();

  if (profile?.role !== "parent") {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  const body = await req.json();
  const { member_id, service_code, requested_date, start_time, end_time, reason_for_absence, notes } = body;

  if (!member_id || !service_code || !requested_date || !start_time || !end_time || !reason_for_absence) {
    return NextResponse.json({ error: "Missing required fields" }, { status: 400 });
  }

  // Verify member belongs to this parent
  const { data: member } = await supabase
    .from("members")
    .select("id, authorized_services")
    .eq("id", member_id)
    .eq("parent_id", user.id)
    .single();

  if (!member) {
    return NextResponse.json({ error: "Member not found" }, { status: 404 });
  }

  if (!member.authorized_services.includes(service_code)) {
    return NextResponse.json({ error: "Service not authorized for this member" }, { status: 400 });
  }

  // Create the request
  const { data: request, error } = await supabase
    .from("coverage_requests")
    .insert({
      member_id,
      parent_id:          user.id,
      service_code,
      requested_date,
      start_time,
      end_time,
      reason_for_absence,
      notes: notes ?? null,
      status: "open",
    })
    .select()
    .single();

  if (error) {
    console.error("[coverage/request] insert error:", error);
    return NextResponse.json({ error: "Failed to create request" }, { status: 500 });
  }

  // Kick off matching asynchronously (don't block the response)
  runMatchingEngine(request.id).catch((err) =>
    console.error("[matching] engine error for request", request.id, err)
  );

  return NextResponse.json({ id: request.id }, { status: 201 });
}
