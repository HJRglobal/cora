"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { format, addDays, startOfDay } from "date-fns";
import { SERVICE_LABELS } from "@/lib/supabase/types";
import type { ServiceType, ProviderAvailability } from "@/lib/supabase/types";

export default function AvailabilityPage() {
  const router = useRouter();
  const [slots, setSlots] = useState<ProviderAvailability[]>([]);
  const [providerServices, setProviderServices] = useState<ServiceType[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);

  // New slot form state
  const [newDate, setNewDate] = useState("");
  const [newStart, setNewStart] = useState("08:00");
  const [newEnd, setNewEnd] = useState("17:00");
  const [newService, setNewService] = useState<ServiceType | "">("");
  const [formError, setFormError] = useState("");

  const today = format(new Date(), "yyyy-MM-dd");
  const maxDate = format(addDays(new Date(), 30), "yyyy-MM-dd");

  useEffect(() => {
    (async () => {
      const supabase = createClient();
      const { data: { user } } = await supabase.auth.getUser();
      if (!user) { router.push("/login"); return; }

      const [{ data: provider }, { data: avail }] = await Promise.all([
        supabase.from("providers").select("service_codes").eq("id", user.id).single(),
        supabase.from("provider_availability")
          .select("*")
          .eq("provider_id", user.id)
          .gte("date", today)
          .order("date")
          .order("start_time"),
      ]);

      setProviderServices(provider?.service_codes ?? []);
      setNewService(provider?.service_codes?.[0] ?? "");
      setSlots(avail ?? []);
      setLoading(false);
    })();
  }, [router, today]);

  async function addSlot() {
    setFormError("");
    if (!newDate || !newStart || !newEnd || !newService) {
      setFormError("Please fill in all fields.");
      return;
    }
    if (newEnd <= newStart) {
      setFormError("End time must be after start time.");
      return;
    }
    setSaving(true);

    const supabase = createClient();
    const { data: { user } } = await supabase.auth.getUser();
    if (!user) return;

    const { data, error } = await supabase
      .from("provider_availability")
      .insert({
        provider_id:  user.id,
        date:         newDate,
        start_time:   newStart,
        end_time:     newEnd,
        service_code: newService as ServiceType,
        is_booked:    false,
      })
      .select()
      .single();

    if (error) {
      setFormError("Could not save this slot. It may overlap with an existing one.");
    } else if (data) {
      setSlots((prev) => [...prev, data].sort((a, b) =>
        a.date.localeCompare(b.date) || a.start_time.localeCompare(b.start_time)
      ));
      setAdding(false);
      setNewDate("");
    }
    setSaving(false);
  }

  async function removeSlot(id: string) {
    const supabase = createClient();
    await supabase.from("provider_availability").delete().eq("id", id);
    setSlots((prev) => prev.filter((s) => s.id !== id));
  }

  function formatSlotTime(t: string) {
    const [h, m] = t.split(":").map(Number);
    const ampm = h >= 12 ? "PM" : "AM";
    const hour = h % 12 || 12;
    return `${hour}:${String(m).padStart(2, "0")} ${ampm}`;
  }

  if (loading) {
    return <div className="page-loader"><div className="w-8 h-8 border-2 border-lex-blue border-t-transparent rounded-full animate-spin" /></div>;
  }

  return (
    <div className="space-y-5">
      <button onClick={() => router.push("/provider/dashboard")}
        className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="m15 18-6-6 6-6"/>
        </svg>
        Back to dashboard
      </button>

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">My availability</h1>
          <p className="text-gray-500 text-sm mt-0.5">Add times you're free to cover last-minute shifts.</p>
        </div>
        {!adding && (
          <Button size="sm" onClick={() => setAdding(true)}>
            + Add slot
          </Button>
        )}
      </div>

      {/* Add slot form */}
      {adding && (
        <Card className="border-lex-blue border-2">
          <CardContent className="p-5 space-y-4">
            <h2 className="font-semibold text-gray-900">New availability slot</h2>

            {providerServices.length > 1 && (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Service type</label>
                <div className="grid grid-cols-2 gap-2">
                  {providerServices.map((s) => (
                    <button key={s} onClick={() => setNewService(s)}
                      className={`p-3 rounded-xl border-2 text-sm font-medium transition-all ${
                        newService === s
                          ? "border-lex-blue bg-lex-blue-light text-lex-blue"
                          : "border-gray-200 text-gray-600 hover:border-gray-300"
                      }`}>
                      {SERVICE_LABELS[s]}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1.5">Date</label>
              <input type="date" min={today} max={maxDate} value={newDate}
                onChange={(e) => setNewDate(e.target.value)}
                className="flex h-12 w-full rounded-xl border-2 border-gray-200 bg-white px-4 text-base focus:outline-none focus:border-lex-blue focus:ring-2 focus:ring-lex-blue/20 transition-colors" />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5">Start</label>
                <input type="time" value={newStart} onChange={(e) => setNewStart(e.target.value)}
                  className="flex h-12 w-full rounded-xl border-2 border-gray-200 bg-white px-4 text-base focus:outline-none focus:border-lex-blue focus:ring-2 focus:ring-lex-blue/20 transition-colors" />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1.5">End</label>
                <input type="time" value={newEnd} onChange={(e) => setNewEnd(e.target.value)}
                  className="flex h-12 w-full rounded-xl border-2 border-gray-200 bg-white px-4 text-base focus:outline-none focus:border-lex-blue focus:ring-2 focus:ring-lex-blue/20 transition-colors" />
              </div>
            </div>

            {formError && <p className="text-sm text-red-600 bg-red-50 rounded-xl px-4 py-2.5">{formError}</p>}

            <div className="flex gap-2 pt-1">
              <Button variant="outline" className="flex-1" onClick={() => { setAdding(false); setFormError(""); }}>
                Cancel
              </Button>
              <Button className="flex-1" disabled={saving} onClick={addSlot}>
                {saving ? "Saving…" : "Save slot"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Existing slots */}
      {slots.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center">
            <div className="w-14 h-14 bg-lex-blue-light rounded-2xl flex items-center justify-center mx-auto mb-4">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#29ABE2" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18M12 14v4M10 16h4"/>
              </svg>
            </div>
            <p className="font-semibold text-gray-900 mb-1">No availability added yet</p>
            <p className="text-sm text-gray-500 mb-5">
              Add the days and times you're free — Lexington will match you with members who need coverage.
            </p>
            <Button onClick={() => setAdding(true)}>Add my first slot</Button>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-2">
          {slots.map((slot) => (
            <Card key={slot.id} className={slot.is_booked ? "opacity-60" : ""}>
              <CardContent className="p-4 flex items-center gap-3">
                <div className="w-10 h-10 rounded-xl bg-lex-green-light flex items-center justify-center flex-shrink-0">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#8DC63F" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>
                  </svg>
                </div>
                <div className="flex-1 min-w-0">
                  <p className="font-semibold text-gray-900 text-sm">
                    {format(new Date(slot.date + "T12:00"), "EEE, MMMM d")}
                  </p>
                  <p className="text-xs text-gray-500">
                    {formatSlotTime(slot.start_time)} – {formatSlotTime(slot.end_time)} &bull; {SERVICE_LABELS[slot.service_code]}
                  </p>
                </div>
                {slot.is_booked ? (
                  <Badge variant="confirmed">Booked</Badge>
                ) : (
                  <button onClick={() => removeSlot(slot.id)}
                    className="text-gray-300 hover:text-red-400 transition-colors p-1">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="3,6 5,6 21,6"/><path d="M19,6v14a2,2,0,0,1-2,2H7a2,2,0,0,1-2-2V6m3,0V4a1,1,0,0,1,1-1h4a1,1,0,0,1,1,1v2"/>
                    </svg>
                  </button>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
