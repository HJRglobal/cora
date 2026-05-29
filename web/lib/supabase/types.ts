// Auto-generate this file from Supabase CLI:
//   npx supabase gen types typescript --project-id YOUR_PROJECT_ID > lib/supabase/types.ts
//
// Until then, these hand-written types mirror the migration exactly.

export type UserRole = "parent" | "provider" | "admin";
export type ServiceType = "hcbs" | "dta";
export type RequestStatus = "open" | "matched" | "confirmed" | "cancelled" | "expired";
export type MatchStatus = "pending" | "accepted" | "declined" | "expired";

export interface Profile {
  id: string;
  role: UserRole;
  full_name: string;
  phone: string;
  email: string | null;
  avatar_url: string | null;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface Provider {
  id: string;
  ahcccs_provider_id: string | null;
  credential_expiry: string | null;
  service_codes: ServiceType[];
  max_weekly_hours: number;
  city: string | null;
  zip_code: string | null;
  bio: string | null;
  background_check_cleared: boolean;
  created_at: string;
  updated_at: string;
  // joined
  profile?: Profile;
}

export interface Member {
  id: string;
  parent_id: string;
  full_name: string;
  ddd_member_id: string | null;
  permanent_provider_id: string | null;
  authorized_services: ServiceType[];
  authorized_hours_remaining: number | null;
  auth_period_end: string | null;
  special_notes: string | null;
  active: boolean;
  created_at: string;
  updated_at: string;
  // joined
  permanent_provider?: Provider & { profile: Profile };
}

export interface ProviderAvailability {
  id: string;
  provider_id: string;
  date: string;
  start_time: string;
  end_time: string;
  service_code: ServiceType;
  is_booked: boolean;
  created_at: string;
}

export interface CoverageRequest {
  id: string;
  member_id: string;
  parent_id: string;
  service_code: ServiceType;
  requested_date: string;
  start_time: string;
  end_time: string;
  reason_for_absence: string;
  status: RequestStatus;
  notes: string | null;
  created_at: string;
  updated_at: string;
  // joined
  member?: Member;
  matches?: CoverageMatch[];
}

export interface CoverageMatch {
  id: string;
  request_id: string;
  provider_id: string;
  availability_id: string | null;
  status: MatchStatus;
  notified_at: string | null;
  responded_at: string | null;
  decline_reason: string | null;
  created_at: string;
  // joined
  provider?: Provider & { profile: Profile };
  request?: CoverageRequest;
}

export type Database = {
  public: {
    Tables: {
      profiles:              { Row: Profile };
      providers:             { Row: Provider };
      members:               { Row: Member };
      provider_availability: { Row: ProviderAvailability };
      coverage_requests:     { Row: CoverageRequest };
      coverage_matches:      { Row: CoverageMatch };
    };
  };
};

// Service type display helpers
export const SERVICE_LABELS: Record<ServiceType, string> = {
  hcbs: "In-Home Support (HCBS)",
  dta:  "Day Treatment & Training (DTA)",
};

export const SERVICE_DESCRIPTIONS: Record<ServiceType, string> = {
  hcbs: "Daily living support provided in the member's home",
  dta:  "Skill-building at a day program or community setting",
};
