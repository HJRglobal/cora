import { createClient } from "@/lib/supabase/server";
import { redirect } from "next/navigation";
import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { format } from "date-fns";
import { formatTime } from "@/lib/utils";
import type { RequestStatus } from "@/lib/supabase/types";
import { SERVICE_LABELS } from "@/lib/supabase/types";

const STATUS_LABELS: Record<RequestStatus, string> = {
  open:      "Searching for provider…",
  matched:   "Provider notified",
  confirmed: "Coverage confirmed",
  cancelled: "Cancelled",
  expired:   "No provider found",
};

export const metadata = { title: "My Coverage" };

export default async function ParentDashboard() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  const { data: profile } = await supabase
    .from("profiles")
    .select("full_name")
    .eq("id", user.id)
    .single();

  const { data: members } = await supabase
    .from("members")
    .select("*, permanent_provider:providers(*, profile:profiles(full_name))")
    .eq("parent_id", user.id)
    .eq("active", true);

  const { data: requests } = await supabase
    .from("coverage_requests")
    .select("*, member:members(full_name), matches:coverage_matches(*, provider:providers(*, profile:profiles(full_name)))")
    .eq("parent_id", user.id)
    .order("requested_date", { ascending: false })
    .limit(10);

  const activeRequests = (requests ?? []).filter(
    (r) => r.status === "open" || r.status === "matched" || r.status === "confirmed"
  );
  const pastRequests = (requests ?? []).filter(
    (r) => r.status === "cancelled" || r.status === "expired"
  );

  const firstName = profile?.full_name?.split(" ")[0] ?? "there";

  return (
    <div className="space-y-6">
      {/* Greeting */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Hi, {firstName}</h1>
        <p className="text-gray-500 text-sm mt-0.5">
          {activeRequests.length > 0
            ? `You have ${activeRequests.length} active coverage request${activeRequests.length !== 1 ? "s" : ""}.`
            : "No active coverage requests."}
        </p>
      </div>

      {/* Quick action */}
      <Link href="/request">
        <div className="bg-lex-blue rounded-2xl p-5 flex items-center gap-4 hover:bg-lex-blue-dark transition-colors cursor-pointer">
          <div className="w-12 h-12 bg-white/20 rounded-xl flex items-center justify-center flex-shrink-0">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10"/><path d="M12 8v8M8 12h8"/>
            </svg>
          </div>
          <div className="flex-1">
            <p className="text-white font-semibold">Request last-minute coverage</p>
            <p className="text-white/75 text-sm">Find an available provider quickly</p>
          </div>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="m9 18 6-6-6-6"/>
          </svg>
        </div>
      </Link>

      {/* Members */}
      {members && members.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-3">
            Your family members
          </h2>
          <div className="space-y-3">
            {members.map((m) => (
              <Card key={m.id}>
                <CardContent className="p-4 flex items-center gap-3">
                  <div className="w-10 h-10 rounded-xl bg-lex-blue-light flex items-center justify-center flex-shrink-0">
                    <span className="text-lex-blue font-bold text-sm">
                      {m.full_name.split(" ").map((n: string) => n[0]).join("").slice(0, 2)}
                    </span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-semibold text-gray-900 truncate">{m.full_name}</p>
                    <p className="text-sm text-gray-500 truncate">
                      Provider:{" "}
                      {(m.permanent_provider as any)?.profile?.full_name ?? "Not assigned"}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {(m.authorized_services as string[]).map((s) => (
                      <Badge key={s} variant="open" className="text-xs">
                        {s.toUpperCase()}
                      </Badge>
                    ))}
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </section>
      )}

      {/* Active requests */}
      {activeRequests.length > 0 && (
        <section>
          <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-3">
            Active requests
          </h2>
          <div className="space-y-3">
            {activeRequests.map((req) => {
              const confirmedMatch = (req.matches ?? []).find(
                (m: any) => m.status === "accepted"
              );
              return (
                <Card key={req.id}>
                  <CardContent className="p-4">
                    <div className="flex items-start justify-between gap-3 mb-3">
                      <div>
                        <p className="font-semibold text-gray-900">
                          {(req.member as any)?.full_name}
                        </p>
                        <p className="text-sm text-gray-500">
                          {SERVICE_LABELS[req.service_code as keyof typeof SERVICE_LABELS]}
                        </p>
                      </div>
                      <Badge variant={req.status as any}>
                        {STATUS_LABELS[req.status as RequestStatus]}
                      </Badge>
                    </div>
                    <div className="flex items-center gap-4 text-sm text-gray-600">
                      <span className="flex items-center gap-1.5">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>
                        </svg>
                        {format(new Date(req.requested_date), "EEE, MMM d")}
                      </span>
                      <span>
                        {formatTime(req.start_time)} – {formatTime(req.end_time)}
                      </span>
                    </div>
                    {confirmedMatch && (
                      <div className="mt-3 pt-3 border-t border-gray-100 flex items-center gap-2">
                        <div className="w-6 h-6 rounded-full bg-lex-green-light flex items-center justify-center">
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#8DC63F" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                            <polyline points="20,6 9,17 4,12"/>
                          </svg>
                        </div>
                        <p className="text-sm text-gray-700">
                          <span className="font-semibold">
                            {(confirmedMatch as any).provider?.profile?.full_name}
                          </span>{" "}
                          will provide coverage
                        </p>
                      </div>
                    )}
                    {req.status === "open" && (
                      <div className="mt-3 pt-3 border-t border-gray-100">
                        <p className="text-sm text-gray-500 flex items-center gap-1.5">
                          <span className="inline-block w-2 h-2 rounded-full bg-lex-blue animate-pulse" />
                          Searching for available providers…
                        </p>
                      </div>
                    )}
                  </CardContent>
                </Card>
              );
            })}
          </div>
        </section>
      )}

      {/* Empty state */}
      {activeRequests.length === 0 && (
        <Card>
          <CardContent className="py-12 text-center">
            <div className="w-14 h-14 bg-lex-blue-light rounded-2xl flex items-center justify-center mx-auto mb-4">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#29ABE2" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8"/>
              </svg>
            </div>
            <p className="font-semibold text-gray-900 mb-1">No active requests</p>
            <p className="text-sm text-gray-500 mb-5">
              Need a provider to step in? We'll find someone fast.
            </p>
            <Link href="/request">
              <Button>Request coverage now</Button>
            </Link>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
