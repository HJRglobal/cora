// Root landing — detect auth state and redirect to the right portal
import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";

export default async function RootPage() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    redirect("/login");
  }

  // Look up role and redirect to the right portal
  const { data: profile } = await supabase
    .from("profiles")
    .select("role")
    .eq("id", user.id)
    .single();

  if (!profile) redirect("/login");

  switch (profile.role) {
    case "parent":   redirect("/dashboard");
    case "provider": redirect("/provider/dashboard");
    case "admin":    redirect("/admin/dashboard");
    default:         redirect("/login");
  }
}
