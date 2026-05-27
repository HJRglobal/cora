import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { Logo } from "@/components/brand/Logo";
import Link from "next/link";

export default async function ParentLayout({ children }: { children: React.ReactNode }) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  const { data: profile } = await supabase
    .from("profiles")
    .select("role, full_name")
    .eq("id", user.id)
    .single();

  if (!profile || profile.role !== "parent") redirect("/login");

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="portal-header">
        <Logo size="sm" />
        <div className="flex items-center gap-4">
          <span className="text-sm text-gray-500 hidden sm:block">
            {profile.full_name}
          </span>
          <Link href="/request" className="hidden sm:block">
            <span className="inline-flex items-center gap-1.5 bg-lex-blue text-white text-sm font-semibold px-4 py-2 rounded-xl hover:bg-lex-blue-dark transition-colors">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" /><path d="M12 8v8M8 12h8" />
              </svg>
              Request Coverage
            </span>
          </Link>
          <form action="/api/auth/signout" method="POST">
            <button className="text-sm text-gray-400 hover:text-gray-600">Sign out</button>
          </form>
        </div>
      </header>

      {/* Bottom mobile nav */}
      <nav className="fixed bottom-0 left-0 right-0 bg-white border-t border-gray-100 flex sm:hidden z-40">
        <Link href="/dashboard" className="flex-1 flex flex-col items-center py-3 gap-1 text-lex-blue">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9,22 9,12 15,12 15,22"/>
          </svg>
          <span className="text-xs font-medium">Home</span>
        </Link>
        <Link href="/request" className="flex-1 flex flex-col items-center py-3 gap-1 text-gray-400">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10"/><path d="M12 8v8M8 12h8"/>
          </svg>
          <span className="text-xs font-medium">Request</span>
        </Link>
        <Link href="/history" className="flex-1 flex flex-col items-center py-3 gap-1 text-gray-400">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10"/><polyline points="12,6 12,12 16,14"/>
          </svg>
          <span className="text-xs font-medium">History</span>
        </Link>
      </nav>

      <main className="max-w-2xl mx-auto px-4 py-6 pb-24 sm:pb-8 animate-fade-in">
        {children}
      </main>
    </div>
  );
}
