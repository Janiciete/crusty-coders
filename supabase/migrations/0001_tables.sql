-- 0001_tables.sql
-- Core schema for the Accessible Campus Navigator (plan §7.2, CLAUDE.md §5).
-- Paste this file into the Supabase SQL editor FIRST, then 0002, 0003, 0004 in order.

-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------
-- Supabase convention: install extensions into the `extensions` schema, not
-- `public`. [VERIFY] that `geography` (and other PostGIS types) resolve
-- without schema-qualification afterwards; Supabase projects normally have
-- `extensions` on the default `search_path` already. If `geography(Point,4326)`
-- below fails to resolve, re-run as `extensions.geography(Point,4326)`.
create extension if not exists postgis with schema extensions;

-- ---------------------------------------------------------------------------
-- profiles
-- ---------------------------------------------------------------------------
create table if not exists public.profiles (
    user_id    uuid primary key references auth.users (id) on delete cascade,
    preferences jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- saved_places
-- ---------------------------------------------------------------------------
create table if not exists public.saved_places (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references auth.users (id) on delete cascade,
    name       text not null,
    location   extensions.geography(Point, 4326) not null,
    created_at timestamptz not null default now()
);

create index if not exists saved_places_user_id_idx on public.saved_places (user_id);

-- ---------------------------------------------------------------------------
-- reports
-- ---------------------------------------------------------------------------
-- No user id column on this table, by design (plan §11.2 / CLAUDE.md §7).
create table if not exists public.reports (
    id            uuid primary key default gen_random_uuid(),
    type          text not null check (type in (
                      'blocked_path', 'ice', 'construction', 'too_steep',
                      'too_dark', 'too_loud', 'crowded'
                  )),
    location      extensions.geography(Point, 4326) not null,
    note          text check (note is null or char_length(note) <= 280),
    photo_path    text,
    created_at    timestamptz not null default now(),
    expires_at    timestamptz,
    confirmations integer not null default 1 check (confirmations >= 0),
    status        text not null default 'pending' check (status in ('pending', 'confirmed', 'expired')),
    source        text not null default 'user' check (source in ('user', 'seed'))
);

create index if not exists reports_location_idx on public.reports using gist (location);
create index if not exists reports_type_status_idx on public.reports (type, status);
create index if not exists reports_expires_at_idx on public.reports (expires_at);

-- ---------------------------------------------------------------------------
-- report_confirmations
-- ---------------------------------------------------------------------------
-- user_hash is SHA-256 hex (64 hex chars); raw user/device id is never stored.
create table if not exists public.report_confirmations (
    report_id  uuid not null references public.reports (id) on delete cascade,
    user_hash  text not null check (user_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz not null default now(),
    unique (report_id, user_hash)
);

create index if not exists report_confirmations_report_id_idx on public.report_confirmations (report_id);
