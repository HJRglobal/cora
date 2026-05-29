import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { Logo } from "@/components/brand/Logo";
import Link from "next/link";

const NAV = [
  { href: "/admin/dashboard", label: "Dashboard", icon: "M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M9 22V12h6v10" },
  { href: "/admin/requests",  label: "Requests",  icon: "M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2 M9 5a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2 M9 5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2 M12 12h.01 M12 16h.01" },
  { href: "/admin/providers", label: "Providers", icon: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 7a4 4 0 1 0 8 0 4 4 0 0 0-8 0 M23 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75" },
  { href: "/admin/members",   label: "Members",   icon: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z" },
];

export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) redirect("/login");

  const { data: profile } = await supabase
    .from("profiles")
    .select("role, full_name")
    .eq("id", user.id)
    .single();

  if (!profile || profile.role !== "admin") redirect("/login");

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Sidebar — desktop */}
      <aside className="hidden sm:flex fixed left-0 top-0 bottom-0 w-56 bg-white border-r border-gray-100 flex-col">
        <div className="p-5 border-b border-gray-100">
          <Logo size="sm" />
          <div className="mt-2">
            <span className="text-xs font-semibold text-lex-purple bg-lex-purple-light px-2 py-0.5 rounded-full">
              Admin
            </span>
          </div>
        </div>
        <nav className="flex-1 py-4 px-3 space-y-1">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium text-gray-600 hover:bg-gray-50 hover:text-gray-900 transition-colors"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                {item.icon.split(" M").map((d, i) => (
                  <path key={i} d={i === 0 ? d : `M${d}`} />
                ))}
              </svg>
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="p-4 border-t border-gray-100">
          <p className="text-xs text-gray-500 truncate mb-2">{profile.full_name}</p>
          <form action="/api/auth/signout" method="POST">
            <button className="text-xs text-gray-400 hover:text-gray-600">Sign out</button>
          </form>
        </div>
      </aside>

      {/* Mobile header */}
      <header className="sm:hidden portal-header">
        <Logo size="sm" />
        <span className="text-xs font-semibold text-lex-purple bg-lex-purple-light px-2 py-0.5 rounded-full">Admin</span>
      </header>

      {/* Mobile bottom nav */}
      <nav className="fixed bottom-0 left-0 right-0 bg-white border-t border-gray-100 flex sm:hidden z-40">
        {NAV.map((item) => (
          <Link key={item.href} href={item.href}
            className="flex-1 flex flex-col items-center py-3 gap-0.5 text-gray-400">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              {item.icon.split(" M").map((d, i) => (
                <path key={i} d={i === 0 ? d : `M${d}`} />
              ))}
            </svg>
            <span className="text-[10px] font-medium">{item.label}</span>
          </Link>
        ))}
      </nav>

      <main className="sm:ml-56 px-4 sm:px-8 py-6 pb-24 sm:pb-8 animate-fade-in">
        {children}
      </main>
    </div>
  );
}
