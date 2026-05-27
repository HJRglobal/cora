import { createClient } from "@/lib/supabase/server";
import { redirect } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import Link from "next/link";
import { format } from "date-fns";
import { formatTime } from "@/lib/utils";
import { SERVICE_LABELS } from "@/lib/supabase/types";
import type { RequestStatus } from "@/lib/supabase/types";

export const metadata = { title: "Admin Dashboard" };

const STATUS_LABELS: Record<RequestStatus, string> = {
  open:      "Searching",
  matched:   "Provider notified",
  confirmed: "Confirmed",
  cancelled: "Cancelled",
  expired:   "Expired",
};

export default async function AdminDashboard() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  const today = format(new Date(), "yyyy-MM-dd");

  // Aggregate stats
  const [
    { count: totalProviders },
    { count: totalMembers },
    { count: openRequests },
    { count: todayConfirmed },
  ] = await Promise.all([
    supabase.from("providers").select("*", { count: "exact", head: true }),
    supabase.from("members").select("*", { count: "exact", head: true }).eq("active", true),
    supabase.from("coverage_requests").select("*", { count: "exact", head: true })
      .in("status", ["open", "matched"]),
    supabase.from("coverage_requests").select("*", { count: "exact", head: true })
      .eq("status", "confirmed").eq("requested_date", today),
  ]);

  // Recent requests
  const { data: recentRequests } = await supabase
    .from("coverage_requests")
    .select("*, member:members(full_name), matches:coverage_matches(status, provider:providers(*, profile:profiles(full_name)))")
    .order("created_at", { ascending: false })
    .limit(8);

  // Providers with credentials expiring soon
  const thirtyDays = format(new Date(Date.now() + 30 * 86400000), "yyyy-MM-dd");
  const { data: expiringCreds } = await supabase
    .from("providers")
    .select("*, profile:profiles(full_name)")
    .lte("credential_expiry", thirtyDays)
    .gte("credential_expiry", today)
    .order("credential_expiry");

  return (
    <div className="space-y-6 max-w-5xl">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Coverage Dashboard</h1>
        <p className="text-gray-500 text-sm mt-0.5">{format(new Date(), "EEEE, MMMM d, yyyy")}</p>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: "Open requests", value: openRequests ?? 0, color: "text-lex-gold", bg: "bg-lex-gold-light" },
          { label: "Confirmed today", value: todayConfirmed ?? 0, color: "text-lex-green", bg: "bg-lex-green-light" },
          { label: "Active providers", value: totalProviders ?? 0, color: "text-lex-blue", bg: "bg-lex-blue-light" },
          { label: "Active members", value: totalMembers ?? 0, color: "text-lex-purple", bg: "bg-lex-purple-light" },
        ].map((stat) => (
          <Card key={stat.label}>
            <CardContent className="p-4">
              <p className={`text-3xl font-bold ${stat.color}`}>{stat.value}</p>
              <p className="text-xs text-gray-500 mt-1">{stat.label}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Credential expiry alerts */}
      {(expiringCreds ?? []).length > 0 && (
        <Card className="border-lex-gold border-2">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#FAC119" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
              </svg>
              <CardTitle className="text-base">Credentials expiring within 30 days</CardTitle>
            </div>
          </CardHeader>
          <CardContent className="pt-0">
            <div className="space-y-2">
              {expiringCreds!.map((p) => (
                <div key={p.id} className="flex items-center justify-between text-sm py-1.5 border-b border-gray-100 last:border-0">
                  <span className="font-medium text-gray-800">{(p as any).profile?.full_name}</span>
                  <span className="text-lex-gold font-semibold">
                    Expires {p.credential_expiry ? format(new Date(p.credential_expiry + "T12:00"), "MMM d") : "—"}
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Recent requests */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide">Recent requests</h2>
          <Link href="/admin/requests" className="text-sm text-lex-blue hover:underline">View all</Link>
        </div>

        {(recentRequests ?? []).length === 0 ? (
          <Card>
            <CardContent className="py-10 text-center text-gray-400 text-sm">
              No coverage requests yet.
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-2">
            {recentRequests!.map((req) => {
              const confirmedMatch = (req.matches ?? []).find((m: any) => m.status === "accepted");
              return (
                <Card key={req.id} className="hover:shadow-md transition-shadow">
                  <CardContent className="p-4">
                    <div className="flex flex-col sm:flex-row sm:items-center gap-2">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <p className="font-semibold text-gray-900 text-sm truncate">
                            {(req.member as any)?.full_name}
                          </p>
                          <Badge variant={req.status as any}>
                            {STATUS_LABELS[req.status as RequestStatus]}
                          </Badge>
                        </div>
                        <p className="text-xs text-gray-500">
                          {SERVICE_LABELS[req.service_code as keyof typeof SERVICE_LABELS]} &bull;{" "}
                          {req.requested_date ? format(new Date(req.requested_date + "T12:00"), "EEE, MMM d") : ""} &bull;{" "}
                          {req.start_time ? `${formatTime(req.start_time)} – ${formatTime(req.end_time)}` : ""}
                        </p>
                        {confirmedMatch && (
                          <p className="text-xs text-lex-green mt-1 font-medium">
                            ✓ {(confirmedMatch as any).provider?.profile?.full_name}
                          </p>
                        )}
                      </div>
                      <div className="flex items-center gap-2 sm:flex-col sm:items-end">
                        <p className="text-xs text-gray-400">
                          {req.created_at ? format(new Date(req.created_at), "h:mm a") : ""}
                        </p>
                        {req.status === "open" && (
                          <Link href={`/admin/requests/${req.id}`}>
                            <Button variant="outline" size="sm">Review</Button>
                          </Link>
                        )}
                      </div>
                    </div>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
