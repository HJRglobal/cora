"use client";

import { useState, useRef, useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Logo } from "@/components/brand/Logo";
import { Button } from "@/components/ui/button";
import { createClient } from "@/lib/supabase/client";
import { displayPhone, formatPhone } from "@/lib/utils";

function VerifyForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const phone = searchParams.get("phone") ?? "";

  const [code, setCode] = useState(["", "", "", "", "", ""]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [resending, setResending] = useState(false);
  const [resent, setResent] = useState(false);
  const inputs = useRef<(HTMLInputElement | null)[]>([]);

  useEffect(() => {
    inputs.current[0]?.focus();
  }, []);

  function handleDigit(idx: number, val: string) {
    const digit = val.replace(/\D/g, "").slice(-1);
    const next = [...code];
    next[idx] = digit;
    setCode(next);
    setError("");

    if (digit && idx < 5) {
      inputs.current[idx + 1]?.focus();
    }

    // Auto-submit when all 6 digits entered
    if (digit && next.every(Boolean)) {
      verify(next.join(""));
    }
  }

  function handleKeyDown(idx: number, e: React.KeyboardEvent) {
    if (e.key === "Backspace" && !code[idx] && idx > 0) {
      inputs.current[idx - 1]?.focus();
    }
  }

  function handlePaste(e: React.ClipboardEvent) {
    const pasted = e.clipboardData.getData("text").replace(/\D/g, "").slice(0, 6);
    if (pasted.length === 6) {
      setCode(pasted.split(""));
      verify(pasted);
    }
  }

  async function verify(token: string) {
    if (loading) return;
    setLoading(true);
    setError("");

    const supabase = createClient();
    const { error: verifyErr } = await supabase.auth.verifyOtp({
      phone,
      token,
      type: "sms",
    });

    if (verifyErr) {
      setError("That code doesn't match. Check the SMS and try again.");
      setCode(["", "", "", "", "", ""]);
      inputs.current[0]?.focus();
      setLoading(false);
      return;
    }

    // Role-based redirect handled by root page
    router.push("/");
  }

  async function resend() {
    setResending(true);
    const supabase = createClient();
    await supabase.auth.signInWithOtp({ phone });
    setResending(false);
    setResent(true);
    setTimeout(() => setResent(false), 30000);
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-lex-blue-light via-white to-lex-purple-light flex flex-col">
      <div className="px-6 pt-6">
        <Logo size="md" />
      </div>

      <div className="flex-1 flex items-center justify-center px-4 py-12">
        <div className="w-full max-w-sm">
          <div className="bg-white rounded-3xl shadow-lg border border-gray-100 p-8">
            <div className="w-14 h-14 rounded-2xl bg-lex-green flex items-center justify-center mb-6">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="2" y="7" width="20" height="14" rx="2" />
                <path d="M16 3H8a2 2 0 0 0-2 2v2h12V5a2 2 0 0 0-2-2Z" />
              </svg>
            </div>

            <h1 className="text-2xl font-bold text-gray-900 mb-1">Enter your code</h1>
            <p className="text-gray-500 text-sm mb-6">
              We sent a 6-digit code to{" "}
              <span className="font-semibold text-gray-700">{displayPhone(phone)}</span>
            </p>

            {/* OTP boxes */}
            <div className="flex gap-2 justify-center mb-6" onPaste={handlePaste}>
              {code.map((digit, idx) => (
                <input
                  key={idx}
                  ref={(el) => { inputs.current[idx] = el; }}
                  className="otp-input"
                  type="text"
                  inputMode="numeric"
                  maxLength={1}
                  value={digit}
                  onChange={(e) => handleDigit(idx, e.target.value)}
                  onKeyDown={(e) => handleKeyDown(idx, e)}
                  disabled={loading}
                />
              ))}
            </div>

            {error && (
              <p className="text-sm text-red-600 bg-red-50 rounded-xl px-4 py-2.5 mb-4">
                {error}
              </p>
            )}

            <Button
              size="lg"
              className="w-full"
              disabled={loading || code.some((d) => !d)}
              onClick={() => verify(code.join(""))}
            >
              {loading ? "Verifying…" : "Verify & sign in"}
            </Button>

            <div className="mt-5 text-center">
              {resent ? (
                <p className="text-sm text-lex-green font-medium">Code resent!</p>
              ) : (
                <button
                  onClick={resend}
                  disabled={resending}
                  className="text-sm text-lex-blue hover:underline disabled:opacity-50"
                >
                  {resending ? "Sending…" : "Didn't get it? Resend code"}
                </button>
              )}
            </div>

            <div className="mt-4 text-center">
              <button
                onClick={() => router.push("/login")}
                className="text-sm text-gray-400 hover:text-gray-600"
              >
                ← Use a different number
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function VerifyPage() {
  return (
    <Suspense>
      <VerifyForm />
    </Suspense>
  );
}
