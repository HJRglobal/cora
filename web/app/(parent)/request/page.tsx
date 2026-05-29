"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Member, ServiceType } from "@/lib/supabase/types";
import { SERVICE_LABELS } from "@/lib/supabase/types";
import { format } from "date-fns";

const ABSENCE_REASONS = [
  "Provider called out sick",
  "Provider has a family emergency",
  "Provider is unavailable — no reason given",
  "Provider notified — scheduling conflict",
  "Other",
];

export default function RequestPage() {
  const router = useRouter();
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  const [selectedMember, setSelectedMember] = useState<Member | null>(null);
  const [serviceCode, setServiceCode] = useState<ServiceType | "">("");
  const [date, setDate] = useState("");
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [reason, setReason] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    (async () => {
      const supabase = createClient();
      const { data: { user } } = await supabase.auth.getUser();
      if (!user) { router.push("/login"); return; }
      const { data } = await supabase
        .from("members")
        .select("*")
        .eq("parent_id", user.id)
        .eq("active", true);
      setMembers(data ?? []);
      setLoading(false);
    })();
  }, [router]);

  async function submit() {
    if (!selectedMember || !serviceCode || !date || !startTime || !endTime || !reason) return;
    setSubmitting(true);
    setError("");

    const res = await fetch("/api/coverage/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        member_id:        selectedMember.id,
        service_code:     serviceCode,
        requested_date:   date,
        start_time:       startTime,
        end_time:         endTime,
        reason_for_absence: reason,
        notes:            notes || null,
      }),
    });

    if (!res.ok) {
      const err = await res.json();
      setError(err.error ?? "Something went wrong. Please try again.");
      setSubmitting(false);
      return;
    }

    router.push("/dashboard?requested=1");
  }

  const today = format(new Date(), "yyyy-MM-dd");

  if (loading) {
    return (
      <div className="page-loader">
        <div className="w-8 h-8 border-2 border-lex-blue border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {/* Back */}
      <button
        onClick={() => step === 1 ? router.push("/dashboard") : setStep((s) => (s - 1) as any)}
        className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="m15 18-6-6 6-6"/>
        </svg>
        {step === 1 ? "Back to dashboard" : "Back"}
      </button>

      <div>
        <h1 className="text-2xl font-bold text-gray-900">Request coverage</h1>
        <p className="text-gray-500 text-sm mt-0.5">Step {step} of 3</p>
      </div>

      {/* Progress bar */}
      <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
        <div
          className="h-full bg-lex-blue rounded-full transition-all duration-300"
          style={{ width: `${(step / 3) * 100}%` }}
        />
      </div>

      {/* Step 1 — Who needs coverage? */}
      {step === 1 && (
        <div className="space-y-4">
          <h2 className="font-semibold text-gray-800">Who needs coverage?</h2>
          {members.length === 0 ? (
            <Card>
              <CardContent className="py-10 text-center text-gray-500">
                No family members on file. Contact Lexington Services to add a member.
              </CardContent>
            </Card>
          ) : (
            members.map((m) => (
              <button
                key={m.id}
                onClick={() => {
                  setSelectedMember(m);
                  setServiceCode(m.authorized_services[0] ?? "");
                  setStep(2);
                }}
                className="w-full text-left"
              >
                <Card className={`transition-all hover:border-lex-blue hover:shadow-md cursor-pointer ${selectedMember?.id === m.id ? "border-lex-blue border-2" : ""}`}>
                  <CardContent className="p-4 flex items-center gap-3">
                    <div className="w-12 h-12 rounded-xl bg-lex-blue-light flex items-center justify-center flex-shrink-0">
                      <span className="text-lex-blue font-bold">
                        {m.full_name.split(" ").map((n: string) => n[0]).join("").slice(0, 2)}
                      </span>
                    </div>
                    <div className="flex-1">
                      <p className="font-semibold text-gray-900">{m.full_name}</p>
                      <p className="text-sm text-gray-500">
                        Services: {m.authorized_services.map((s) => SERVICE_LABELS[s]).join(", ")}
                      </p>
                    </div>
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#9CA3AF" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="m9 18 6-6-6-6"/>
                    </svg>
                  </CardContent>
                </Card>
              </button>
            ))
          )}
        </div>
      )}

      {/* Step 2 — Service + date/time */}
      {step === 2 && selectedMember && (
        <div className="space-y-5">
          <h2 className="font-semibold text-gray-800">
            Coverage details for {selectedMember.full_name}
          </h2>

          {/* Service type */}
          {selectedMember.authorized_services.length > 1 && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">Service type</label>
              <div className="grid grid-cols-2 gap-2">
                {selectedMember.authorized_services.map((s) => (
                  <button
                    key={s}
                    onClick={() => setServiceCode(s)}
                    className={`p-3 rounded-xl border-2 text-sm font-medium text-left transition-all ${
                      serviceCode === s
                        ? "border-lex-blue bg-lex-blue-light text-lex-blue"
                        : "border-gray-200 text-gray-600 hover:border-gray-300"
                    }`}
                  >
                    {SERVICE_LABELS[s]}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Date */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1.5">Date needed</label>
            <input
              type="date"
              min={today}
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className="flex h-12 w-full rounded-xl border-2 border-gray-200 bg-white px-4 py-2 text-base focus:outline-none focus:border-lex-blue focus:ring-2 focus:ring-lex-blue/20 transition-colors"
            />
          </div>

          {/* Time */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1.5">Start time</label>
              <input
                type="time"
                value={startTime}
                onChange={(e) => setStartTime(e.target.value)}
                className="flex h-12 w-full rounded-xl border-2 border-gray-200 bg-white px-4 py-2 text-base focus:outline-none focus:border-lex-blue focus:ring-2 focus:ring-lex-blue/20 transition-colors"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1.5">End time</label>
              <input
                type="time"
                value={endTime}
                onChange={(e) => setEndTime(e.target.value)}
                className="flex h-12 w-full rounded-xl border-2 border-gray-200 bg-white px-4 py-2 text-base focus:outline-none focus:border-lex-blue focus:ring-2 focus:ring-lex-blue/20 transition-colors"
              />
            </div>
          </div>

          <Button
            size="lg"
            className="w-full"
            disabled={!serviceCode || !date || !startTime || !endTime}
            onClick={() => setStep(3)}
          >
            Continue
          </Button>
        </div>
      )}

      {/* Step 3 — Reason + confirm */}
      {step === 3 && (
        <div className="space-y-5">
          <h2 className="font-semibold text-gray-800">Why is coverage needed?</h2>

          <div className="space-y-2">
            {ABSENCE_REASONS.map((r) => (
              <button
                key={r}
                onClick={() => setReason(r)}
                className={`w-full text-left p-4 rounded-xl border-2 text-sm transition-all ${
                  reason === r
                    ? "border-lex-blue bg-lex-blue-light text-lex-blue font-medium"
                    : "border-gray-200 text-gray-700 hover:border-gray-300"
                }`}
              >
                {r}
              </button>
            ))}
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1.5">
              Additional notes <span className="text-gray-400 font-normal">(optional)</span>
            </label>
            <textarea
              rows={3}
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Anything the provider should know before arriving…"
              className="w-full rounded-xl border-2 border-gray-200 px-4 py-3 text-sm focus:outline-none focus:border-lex-blue focus:ring-2 focus:ring-lex-blue/20 resize-none transition-colors"
            />
          </div>

          {/* Summary card */}
          <Card className="bg-lex-blue-light border-lex-blue/20">
            <CardContent className="p-4 space-y-1.5">
              <p className="text-xs font-semibold text-lex-blue uppercase tracking-wide mb-2">Request summary</p>
              <p className="text-sm text-gray-800"><span className="text-gray-500">Member:</span> {selectedMember?.full_name}</p>
              <p className="text-sm text-gray-800"><span className="text-gray-500">Service:</span> {serviceCode ? SERVICE_LABELS[serviceCode] : ""}</p>
              <p className="text-sm text-gray-800"><span className="text-gray-500">Date:</span> {date ? format(new Date(date + "T12:00"), "EEEE, MMMM d, yyyy") : ""}</p>
              <p className="text-sm text-gray-800"><span className="text-gray-500">Time:</span> {startTime} – {endTime}</p>
            </CardContent>
          </Card>

          {error && (
            <p className="text-sm text-red-600 bg-red-50 rounded-xl px-4 py-3">{error}</p>
          )}

          <Button
            size="lg"
            className="w-full"
            disabled={!reason || submitting}
            onClick={submit}
          >
            {submitting ? "Submitting…" : "Submit coverage request"}
          </Button>

          <p className="text-xs text-center text-gray-400">
            We'll search for an available provider immediately and notify you by text.
          </p>
        </div>
      )}
    </div>
  );
}
