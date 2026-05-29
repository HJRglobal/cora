-- ============================================================
-- Lexington Coverage Portal — Initial Schema
-- ============================================================
-- Run this in Supabase SQL Editor (or via supabase db push)
-- ============================================================

-- Enable UUID extension
create extension if not exists "uuid-ossp";

-- ─── ENUM TYPES ──────────────────────────────────────────────

create type user_role as enum ('parent', 'provider', 'admin');
create type service_type as enum ('hcbs', 'dta');
create type request_status as enum (
  'open',       -- submitted, searching for provider
  'matched',    -- provider found, awaiting acceptance
  'confirmed',  -- provider accepted, coverage booked
  'cancelled',  -- cancelled by parent or admin
  'expired'     -- no provider found in time window
);
create type match_status as enum ('pending', 'accepted', 'declined', 'expired');

-- ─── PROFILES ────────────────────────────────────────────────
-- Extends auth.users (managed by Supabase Auth / phone OTP)

create table profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  role        user_role not null,
  full_name   text not null,
  phone       text not null,
  email       text,
  avatar_url  text,
  active      boolean not null default true,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

alter table profiles enable row level security;

create policy "Users can read own profile"
  on profiles for select using (auth.uid() = id);

create policy "Users can update own profile"
  on profiles for update using (auth.uid() = id);

create policy "Admins can read all profiles"
  on profiles for select
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

-- ─── SERVICE TYPES ───────────────────────────────────────────

create table service_types (
  id          uuid primary key default uuid_generate_v4(),
  code        service_type not null unique,
  label       text not null,
  description text,
  active      boolean not null default true
);

insert into service_types (code, label, description) values
  ('hcbs', 'In-Home Support (HCBS)',
   'Home and Community Based Services — daily living support in the member''s home'),
  ('dta',  'Day Treatment & Training (DTA)',
   'Day program services, skill-building in a community or facility setting');

-- ─── PROVIDERS ───────────────────────────────────────────────

create table providers (
  id                  uuid primary key references profiles(id) on delete cascade,
  ahcccs_provider_id  text,                          -- AHCCCS credential number
  credential_expiry   date,                          -- when credential expires
  service_codes       service_type[] not null default '{}', -- what they're authorized for
  max_weekly_hours    numeric(5,2) not null default 40,
  city                text,
  zip_code            text,
  bio                 text,                          -- short provider bio shown to parents
  background_check_cleared boolean not null default false,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

alter table providers enable row level security;

create policy "Providers can read/update own record"
  on providers for all using (auth.uid() = id);

create policy "Admins can read all providers"
  on providers for select
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

create policy "Admins can update providers"
  on providers for update
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

-- ─── MEMBERS ─────────────────────────────────────────────────

create table members (
  id                    uuid primary key default uuid_generate_v4(),
  parent_id             uuid not null references profiles(id) on delete cascade,
  full_name             text not null,
  ddd_member_id         text,                        -- DDD-assigned member ID
  permanent_provider_id uuid references providers(id),
  authorized_services   service_type[] not null default '{}',
  authorized_hours_remaining numeric(6,2),           -- hours left in current auth period
  auth_period_end       date,
  special_notes         text,                        -- non-PHI operational notes only
  active                boolean not null default true,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

alter table members enable row level security;

create policy "Parents can read own members"
  on members for select using (auth.uid() = parent_id);

create policy "Parents can insert members"
  on members for insert with check (auth.uid() = parent_id);

create policy "Parents can update own members"
  on members for update using (auth.uid() = parent_id);

create policy "Admins can read all members"
  on members for select
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

create policy "Admins can update members"
  on members for update
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

-- ─── PROVIDER AVAILABILITY ───────────────────────────────────

create table provider_availability (
  id           uuid primary key default uuid_generate_v4(),
  provider_id  uuid not null references providers(id) on delete cascade,
  date         date not null,
  start_time   time not null,
  end_time     time not null,
  service_code service_type not null,
  is_booked    boolean not null default false,
  created_at   timestamptz not null default now(),
  constraint no_overlap unique (provider_id, date, start_time)
);

alter table provider_availability enable row level security;

create policy "Providers manage own availability"
  on provider_availability for all using (auth.uid() = provider_id);

create policy "Admins can read all availability"
  on provider_availability for select
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

-- Allow matching engine (service role) to read availability
-- Service role bypasses RLS by default in Supabase

-- ─── COVERAGE REQUESTS ───────────────────────────────────────

create table coverage_requests (
  id                uuid primary key default uuid_generate_v4(),
  member_id         uuid not null references members(id),
  parent_id         uuid not null references profiles(id),
  service_code      service_type not null,
  requested_date    date not null,
  start_time        time not null,
  end_time          time not null,
  reason_for_absence text not null,               -- why permanent provider can't make it
  status            request_status not null default 'open',
  notes             text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

alter table coverage_requests enable row level security;

create policy "Parents can create and read own requests"
  on coverage_requests for all using (auth.uid() = parent_id);

create policy "Admins can read all requests"
  on coverage_requests for select
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

create policy "Admins can update requests"
  on coverage_requests for update
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

-- ─── COVERAGE MATCHES ────────────────────────────────────────

create table coverage_matches (
  id            uuid primary key default uuid_generate_v4(),
  request_id    uuid not null references coverage_requests(id) on delete cascade,
  provider_id   uuid not null references providers(id),
  availability_id uuid references provider_availability(id),
  status        match_status not null default 'pending',
  notified_at   timestamptz,                      -- when provider was alerted via SMS
  responded_at  timestamptz,
  decline_reason text,
  created_at    timestamptz not null default now()
);

alter table coverage_matches enable row level security;

create policy "Providers can read own matches"
  on coverage_matches for select
  using (auth.uid() = provider_id);

create policy "Providers can update own matches"
  on coverage_matches for update
  using (auth.uid() = provider_id);

create policy "Admins can read all matches"
  on coverage_matches for select
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

create policy "Admins can update matches"
  on coverage_matches for update
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

-- ─── NOTIFICATIONS LOG ───────────────────────────────────────

create table notifications_log (
  id          uuid primary key default uuid_generate_v4(),
  recipient   text not null,                      -- phone number (no PII beyond phone)
  channel     text not null default 'sms',
  message     text not null,
  related_request_id uuid references coverage_requests(id),
  sent_at     timestamptz not null default now(),
  success     boolean not null default true,
  error_msg   text
);

-- Admins only — audit trail
alter table notifications_log enable row level security;

create policy "Admins can read notification log"
  on notifications_log for select
  using (exists (
    select 1 from profiles p where p.id = auth.uid() and p.role = 'admin'
  ));

-- ─── REAL-TIME SUBSCRIPTIONS ─────────────────────────────────
-- Enable Supabase Realtime for live status updates in the UI

alter publication supabase_realtime add table coverage_requests;
alter publication supabase_realtime add table coverage_matches;
alter publication supabase_realtime add table provider_availability;

-- ─── UPDATED_AT TRIGGERS ─────────────────────────────────────

create or replace function update_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger profiles_updated_at
  before update on profiles
  for each row execute function update_updated_at();

create trigger providers_updated_at
  before update on providers
  for each row execute function update_updated_at();

create trigger members_updated_at
  before update on members
  for each row execute function update_updated_at();

create trigger coverage_requests_updated_at
  before update on coverage_requests
  for each row execute function update_updated_at();

-- ─── INDEXES ─────────────────────────────────────────────────

create index idx_provider_availability_date     on provider_availability(date, service_code) where not is_booked;
create index idx_coverage_requests_status       on coverage_requests(status, requested_date);
create index idx_coverage_requests_parent       on coverage_requests(parent_id);
create index idx_coverage_matches_provider      on coverage_matches(provider_id, status);
create index idx_members_parent                 on members(parent_id);
