"use client";

import { useState, useEffect, use } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Logo } from "@/components/brand/Logo";
import { format } from "date-fns";
import { formatTime } from "@/lib/utils";
import { SERVICE_LABELS } from "@/lib/supabase/types";

export default function RespondPage({ params }: { params: Promise<{ matchId: string }> }) {
  const { matchId } = use(params);
  const router = useRouter();
  const searchParams = useSearchParams();
  const initialAction = searchParams.get("action") as "accept" | "decline" | null;

  const [match, setMatch] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState<"accepted" | "declined" | null>(null);
  const [declineReason, setDeclineReason] = useState("");

  useEffect(() => {
    (async () => {
      const supabase = createClient();
      const { data } = await supabase
        .from("coverage_matches")
        .select("*, request:coverage_requests(*)")
        .eq("id", matchId)
        .single();
      setMatch(data);
      setLoading(false);
    })();
  }, [matchId]);

  async function respond(action: "accept" | "decline") {
    setSubmitting(true);
    const res = await fetch("/api/coverage/respond", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ match_id: matchId, action, decline_reason: declineReason || null }),
    });
    if (res.ok) {
      setDone(action === "accept" ? "accepted" : "declined");
    }
    setSubmitting(false);
  }

  if (loading) return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="w-8 h-8 border-2 border-lex-blue border-t-transparent rounded-full animate-spin" />
    </div>
  );

  return (
    <div className="min-h-screen bg-gradient-to-br from-lex-blue-light via-white to-lex-green-light flex flex-col">
      <div className="px-6 pt-6"><Logo size="md" /></div>
      <div className="flex-1 flex items-center justify-center px-4 py-12">
        <div className="w-full max-w-sm space-y-4">

          {done === "accepted" && (
            <Card>
              <CardContent className="py-10 text-center">
                <div className="w-16 h-16 bg-lex-green-light rounded-2xl flex items-center justify-center mx-auto mb-4">
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#8DC63F" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="20,6 9,17 4,12"/>
                  </svg>
                </div>
                <h1 className="text-xl font-bold text-gray-900 mb-2">Shift confirmed!</h1>
                <p className="text-gray-500 text-sm mb-5">
                  The family has been notified. Thank you for stepping in.
                </p>
                <Button onClick={() => router.push("/provider/dashboard")}>Go to dashboard</Button>
              </CardContent>
            </Card>
          )}

          {done === "declined" && (
            <Card>
              <CardContent className="py-10 text-center">
                <div className="w-16 h-16 bg-gray-100 rounded-2xl flex items-center justify-center mx-auto mb-4">
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#9CA3AF" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
                  </svg>
                </div>
                <h1 className="text-xl font-bold text-gray-900 mb-2">Declined</h1>
                <p className="text-gray-500 text-sm mb-5">
                  No problem. Lexington will reach out to another provider.
                </p>
                <Button variant="outline" onClick={() => router.push("/provider/dashboard")}>
                  Back to dashboard
                </Button>
              </CardContent>
            </Card>
          )}

          {!done && match && (
            <>
              <Card>
                <CardContent className="p-5">
                  <div className="flex items-center gap-3 mb-4">
                    <div className="w-12 h-12 bg-lex-gold-light rounded-xl flex items-center justify-center flex-shrink-0">
                      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#FAC119" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>
                      </svg>
                    </div>
                    <div>
                      <p className="font-bold text-gray-900">Coverage request</p>
                      <p className="text-sm text-gray-500">Lexington Services</p>
                    </div>
                  </div>

                  <div className="space-y-2.5 text-sm">
                    <div className="flex justify-between">
                      <span className="text-gray-500">Service</span>
                      <span className="font-medium text-gray-800">
                        {SERVICE_LABELS[match.request?.service_code as keyof typeof SERVICE_LABELS]}
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-500">Date</span>
                      <span className="font-medium text-gray-800">
                        {match.request?.requested_date
                          ? format(new Date(match.request.requested_date + "T12:00"), "EEEE, MMMM d")
                          : "—"}
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-500">Time</span>
                      <span className="font-medium text-gray-800">
                        {match.request?.start_time
                          ? `${formatTime(match.request.start_time)} – ${formatTime(match.request.end_time)}`
                          : "—"}
                      </span>
                    </div>
                  </div>
                </CardContent>
              </Card>

              {initialAction === "decline" && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1.5">
                    Reason for declining <span className="text-gray-400">(optional)</span>
                  </label>
                  <textarea
                    rows={3}
                    value={declineReason}
                    onChange={(e) => setDeclineReason(e.target.value)}
                    placeholder="e.g. Already have another commitment"
                    className="w-full rounded-xl border-2 border-gray-200 px-4 py-3 text-sm focus:outline-none focus:border-lex-blue focus:ring-2 focus:ring-lex-blue/20 resize-none"
                  />
                </div>
              )}

              <div className="grid grid-cols-2 gap-3">
                <Button
                  variant="outline"
                  size="lg"
                  className="w-full border-red-200 text-red-600 hover:bg-red-50"
                  disabled={submitting}
                  onClick={() => respond("decline")}
                >
                  Decline
                </Button>
                <Button
                  variant="success"
                  size="lg"
                  className="w-full"
                  disabled={submitting}
                  onClick={() => respond("accept")}
                >
                  {submitting ? "Confirming…" : "Accept shift"}
                </Button>
              </div>

              <p className="text-xs text-center text-gray-400">
                By accepting, you confirm availability for this shift. Contact Lexington Services with any questions.
              </p>
            </>
          )}

          {!done && !match && (
            <Card>
              <CardContent className="py-10 text-center text-gray-500">
                This request is no longer available — it may have already been filled.
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
