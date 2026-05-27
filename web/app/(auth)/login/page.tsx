"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Logo } from "@/components/brand/Logo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createClient } from "@/lib/supabase/client";
import { formatPhone } from "@/lib/utils";

export default function LoginPage() {
  const router = useRouter();
  const [phone, setPhone] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);

    const e164 = formatPhone(phone);
    const supabase = createClient();

    const { error: otpErr } = await supabase.auth.signInWithOtp({ phone: e164 });

    if (otpErr) {
      setError("Couldn't send a verification code. Check the number and try again.");
      setLoading(false);
      return;
    }

    router.push(`/verify?phone=${encodeURIComponent(e164)}`);
  }

  function handlePhoneChange(e: React.ChangeEvent<HTMLInputElement>) {
    // Allow only digits, spaces, dashes, parens, plus
    const val = e.target.value.replace(/[^\d\s\-().+]/g, "");
    setPhone(val);
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-lex-blue-light via-white to-lex-purple-light flex flex-col">
      {/* Top bar */}
      <div className="px-6 pt-6">
        <Logo size="md" />
      </div>

      {/* Center card */}
      <div className="flex-1 flex items-center justify-center px-4 py-12">
        <div className="w-full max-w-sm">
          <div className="bg-white rounded-3xl shadow-lg border border-gray-100 p-8">
            {/* Icon */}
            <div className="w-14 h-14 rounded-2xl bg-lex-blue flex items-center justify-center mb-6">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 12a19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 3.6 1.2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 8.77a16 16 0 0 0 6 6l1.84-1.84a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 15z" />
              </svg>
            </div>

            <h1 className="text-2xl font-bold text-gray-900 mb-1">Sign in</h1>
            <p className="text-gray-500 text-sm mb-6">
              Enter your mobile number and we'll text you a verification code.
            </p>

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label htmlFor="phone" className="block text-sm font-medium text-gray-700 mb-1.5">
                  Mobile number
                </label>
                <Input
                  id="phone"
                  type="tel"
                  inputMode="tel"
                  autoComplete="tel"
                  placeholder="(602) 555-0100"
                  value={phone}
                  onChange={handlePhoneChange}
                  required
                  autoFocus
                />
              </div>

              {error && (
                <p className="text-sm text-red-600 bg-red-50 rounded-xl px-4 py-2.5">
                  {error}
                </p>
              )}

              <Button
                type="submit"
                size="lg"
                className="w-full"
                disabled={loading || phone.replace(/\D/g, "").length < 10}
              >
                {loading ? "Sending code…" : "Send verification code"}
              </Button>
            </form>

            <p className="mt-6 text-xs text-center text-gray-400">
              By signing in, you agree to receive SMS messages from Lexington Services.
              Message and data rates may apply.
            </p>
          </div>

          <p className="mt-6 text-center text-sm text-gray-500">
            Need access?{" "}
            <a href="mailto:info@lexingtonservices.com" className="text-lex-blue font-medium hover:underline">
              Contact Lexington Services
            </a>
          </p>
        </div>
      </div>
    </div>
  );
}
