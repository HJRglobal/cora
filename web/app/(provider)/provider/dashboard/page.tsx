import { createClient } from "@/lib/supabase/server";
import { redirect } from "next/navigation";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import Link from "next/link";
import { format } from "date-fns";
import { formatTime } from "@/lib/utils";
import { SERVICE_LABELS } from "@/lib/supabase/types";

export const metadata = { title: "Provider Dashboard" };

export default async function ProviderDashboard() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  const { data: profile } = await supabase
    .from("profiles")
    .select("full_name")
    .eq("id", user.id)
    .single();

  // Pending match requests (provider needs to respond)
  const { data: pendingMatches } = await supabase
    .from("coverage_matches")
    .select("*, request:coverage_requests(*, member:members(full_name))")
    .eq("provider_id", user.id)
    .eq("status", "pending")
    .order("created_at", { ascending: false });

  // Upcoming confirmed shifts
  const { data: confirmedMatches } = await supabase
    .from("coverage_matches")
    .select("*, request:coverage_requests(*, member:members(full_name))")
    .eq("provider_id", user.id)
    .eq("status", "accepted")
    .gte("request.requested_date" as any, format(new Date(), "yyyy-MM-dd"))
    .order("created_at", { ascending: false })
    .limit(5);

  // Upcoming availability slots
  const { data: availability } = await supabase
    .from("provider_availability")
    .select("*")
    .eq("provider_id", user.id)
    .eq("is_booked", false)
    .gte("date", format(new Date(), "yyyy-MM-dd"))
    .order("date")
    .limit(5);

  const firstName = profile?.full_name?.split(" ")[0] ?? "there";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Hi, {firstName}</h1>
        <p className="text-gray-500 text-sm mt-0.5">
          {(pendingMatches ?? []).length > 0
            ? `You have ${pendingMatches!.length} coverage request${pendingMatches!.length !== 1 ? "s" : ""} waiting for your response.`
            : "No pending requests right now."}
        </p>
      </div>

      {/* Pending requests — high priority */}
      {(pendingMatches ?? []).length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-3">
            Action needed
          </h2>
          <div className="space-y-3">
            {pendingMatches!.map((match) => {
              const req = match.request as any;
              return (
                <Card key={match.id} className="border-lex-gold border-2">
                  <CardContent className="p-4">
                    <div className="flex items-start gap-3 mb-3">
                      <div className="w-10 h-10 rounded-xl bg-lex-gold-light flex items-center justify-center flex-shrink-0 mt-0.5">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#FAC119" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>
                        </svg>
                      </div>
                      <div className="flex-1">
                        <p className="font-semibold text-gray-900">Coverage request</p>
                        <p className="text-sm text-gray-500">
                          {SERVICE_LABELS[req?.service_code as keyof typeof SERVICE_LABELS]}
                        </p>
                      </div>
                      <Badge variant="matched">Respond needed</Badge>
                    </div>
                    <div className="space-y-1 text-sm text-gray-700 mb-4">
                      <div className="flex gap-2">
                        <span className="text-gray-400 w-12">Date</span>
                        <span className="font-medium">
                          {req?.requested_date ? format(new Date(req.requested_date + "T12:00"), "EEE, MMM d") : "—"}
                        </span>
                      </div>
                      <div className="flex gap-2">
                        <span className="text-gray-400 w-12">Time</span>
                        <span className="font-medium">
                          {req?.start_time ? `${formatTime(req.start_time)} – ${formatTime(req.end_time)}` : "—"}
                        </span>
                      </div>
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <Link href={`/provider/respond/${match.id}?action=decline`}>
                        <Button variant="outline" size="sm" className="w-full border-red-200 text-red-600 hover:bg-red-50">
                          Decline
                        </Button>
                      </Link>
                      <Link href={`/provider/respond/${match.id}?action=accept`}>
                        <Button variant="success" size="sm" className="w-full">
                          Accept shift
                        </Button>
                      </Link>
                    </div>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        </section>
      )}

      {/* Set availability CTA */}
      <Link href="/provider/availability">
        <div className={`rounded-2xl p-5 flex items-center gap-4 transition-colors cursor-pointer ${
          (availability ?? []).length === 0
            ? "bg-lex-blue hover:bg-lex-blue-dark"
            : "bg-gray-100 hover:bg-gray-200"
        }`}>
          <div className={`w-12 h-12 rounded-xl flex items-center justify-center flex-shrink-0 ${
            (availability ?? []).length === 0 ? "bg-white/20" : "bg-white"
          }`}>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
              stroke={(availability ?? []).length === 0 ? "white" : "#6B7280"}
              strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18M8 14h.01M12 14h.01M16 14h.01M8 18h.01M12 18h.01"/>
            </svg>
          </div>
          <div className="flex-1">
            <p className={`font-semibold ${(availability ?? []).length === 0 ? "text-white" : "text-gray-800"}`}>
              {(availability ?? []).length === 0 ? "Add your availability" : "Manage availability"}
            </p>
            <p className={`text-sm ${(availability ?? []).length === 0 ? "text-white/75" : "text-gray-500"}`}>
              {(availability ?? []).length === 0
                ? "Let Lexington know when you're free for coverage shifts"
                : `${availability!.length} open slot${availability!.length !== 1 ? "s" : ""} this week`}
            </p>
          </div>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
            stroke={(availability ?? []).length === 0 ? "white" : "#9CA3AF"}
            strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="m9 18 6-6-6-6"/>
          </svg>
        </div>
      </Link>

      {/* Upcoming confirmed shifts */}
      {(confirmedMatches ?? []).length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-3">
            Your upcoming shifts
          </h2>
          <div className="space-y-3">
            {confirmedMatches!.map((match) => {
              const req = match.request as any;
              return (
                <Card key={match.id}>
                  <CardContent className="p-4 flex items-center gap-3">
                    <div className="w-10 h-10 rounded-xl bg-lex-green-light flex items-center justify-center flex-shrink-0">
                      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#8DC63F" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="20,6 9,17 4,12"/>
                      </svg>
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="font-semibold text-gray-900 text-sm">
                        {req?.requested_date ? format(new Date(req.requested_date + "T12:00"), "EEE, MMM d") : "—"}
                      </p>
                      <p className="text-xs text-gray-500">
                        {req?.start_time ? `${formatTime(req.start_time)} – ${formatTime(req.end_time)}` : "—"} &bull; {SERVICE_LABELS[req?.service_code as keyof typeof SERVICE_LABELS]}
                      </p>
                    </div>
                    <Badge variant="confirmed">Confirmed</Badge>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
