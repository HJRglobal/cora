import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { Logo } from "@/components/brand/Logo";
import Link from "next/link";

export default async function ProviderLayout({ children }: { children: React.ReactNode }) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  const { data: profile } = await supabase
    .from("profiles")
    .select("role, full_name")
    .eq("id", user.id)
    .single();

  if (!profile || profile.role !== "provider") redirect("/login");

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="portal-header">
        <Logo size="sm" />
        <div className="flex items-center gap-4">
          <span className="text-sm text-gray-500 hidden sm:block">{profile.full_name}</span>
          <form action="/api/auth/signout" method="POST">
            <button className="text-sm text-gray-400 hover:text-gray-600">Sign out</button>
          </form>
        </div>
      </header>

      {/* Bottom mobile nav */}
      <nav className="fixed bottom-0 left-0 right-0 bg-white border-t border-gray-100 flex sm:hidden z-40">
        <Link href="/provider/dashboard" className="flex-1 flex flex-col items-center py-3 gap-1 text-lex-blue">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9,22 9,12 15,12 15,22"/>
          </svg>
          <span className="text-xs font-medium">Home</span>
        </Link>
        <Link href="/provider/availability" className="flex-1 flex flex-col items-center py-3 gap-1 text-gray-400">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>
          </svg>
          <span className="text-xs font-medium">Availability</span>
        </Link>
        <Link href="/provider/shifts" className="flex-1 flex flex-col items-center py-3 gap-1 text-gray-400">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="9,11 12,14 22,4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
          </svg>
          <span className="text-xs font-medium">Shifts</span>
        </Link>
      </nav>

      <main className="max-w-2xl mx-auto px-4 py-6 pb-24 sm:pb-8 animate-fade-in">
        {children}
      </main>
    </div>
  );
}
