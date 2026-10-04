-- 0003_triggers.sql
-- Rounding, expiry, confirmation logic, and the active_reports view.
-- Paste after 0002_rls.sql.
--
-- All functions are SECURITY DEFINER with a fixed search_path so they cannot
-- be hijacked by a search_path change, and so they can write to tables the
-- calling (possibly anon) role could not otherwise mutate directly.

-- ---------------------------------------------------------------------------
-- Expiry lookup (CLAUDE.md §6): ice 6h, blocked_path 24h, construction 7d,
-- too_dark/too_loud/crowded 2h, too_steep 7d.
-- ---------------------------------------------------------------------------
create or replace function public.report_expiry_interval(p_type text)
returns interval
language sql
immutable
set search_path = pg_catalog, public
as $$
    select case p_type
        when 'ice'           then interval '6 hours'
        when 'blocked_path'  then interval '24 hours'
        when 'construction'  then interval '7 days'
        when 'too_steep'     then interval '7 days'
        when 'too_dark'      then interval '2 hours'
        when 'too_loud'      then interval '2 hours'
        when 'crowded'       then interval '2 hours'
        else interval '2 hours'
    end;
$$;

-- ---------------------------------------------------------------------------
-- BEFORE INSERT on reports: round location to 4 decimal places (~10 m) and
-- enforce client-vs-service-role fields.
--
-- [VERIFY] auth.role() reflects the PostgREST calling role ('anon',
-- 'authenticated', or 'service_role') inside a SECURITY DEFINER trigger.
-- This relies on Supabase's convention that auth.role() reads the JWT role
-- claim via a GUC (current_setting('request.jwt.claims', true)), which is
-- unaffected by SECURITY DEFINER's search_path change, not on table
-- ownership or search_path. If this assumption is wrong, seed rows inserted
-- with the service key would incorrectly be forced into pending/user status.
-- ---------------------------------------------------------------------------
create or replace function public.reports_before_insert()
returns trigger
language plpgsql
security definer
set search_path = public, extensions, pg_catalog
as $$
declare
    v_role text := auth.role();
begin
    -- Always round to ~10 m, regardless of caller.
    new.location := extensions.ST_SetSRID(
        extensions.ST_MakePoint(
            round(extensions.ST_X(new.location::extensions.geometry)::numeric, 4)::float8,
            round(extensions.ST_Y(new.location::extensions.geometry)::numeric, 4)::float8
        ),
        4326
    )::extensions.geography;

    if v_role is distinct from 'service_role' then
        -- Client-originated insert: force server-controlled fields.
        new.source := 'user';
        new.status := 'pending';
        new.confirmations := 1;
        new.expires_at := now() + public.report_expiry_interval(new.type);
    elsif new.source = 'seed' then
        -- Service-role seed load: confirmed, never expires.
        new.status := 'confirmed';
        new.expires_at := null;
        new.confirmations := coalesce(new.confirmations, 1);
    end if;

    return new;
end;
$$;

drop trigger if exists trg_reports_before_insert on public.reports;
create trigger trg_reports_before_insert
    before insert on public.reports
    for each row
    execute function public.reports_before_insert();

-- ---------------------------------------------------------------------------
-- AFTER INSERT on reports: independent-confirmation rule (CLAUDE.md §6 /
-- plan §10.4). If another non-expired report of the same type lies within
-- 15 m and was created within 60 minutes, confirm both.
-- ---------------------------------------------------------------------------
create or replace function public.reports_after_insert()
returns trigger
language plpgsql
security definer
set search_path = public, extensions, pg_catalog
as $$
declare
    v_match_id uuid;
begin
    select r.id into v_match_id
    from public.reports r
    where r.id <> new.id
      and r.type = new.type
      and r.status <> 'expired'
      and (r.expires_at is null or r.expires_at > now())
      and r.created_at >= new.created_at - interval '60 minutes'
      and r.created_at <= new.created_at + interval '60 minutes'
      and extensions.ST_DWithin(r.location, new.location, 15)
    order by r.created_at desc
    limit 1;

    if v_match_id is not null then
        update public.reports
            set status = 'confirmed',
                confirmations = greatest(confirmations + 1, 2)
            where id = v_match_id;

        update public.reports
            set status = 'confirmed',
                confirmations = greatest(confirmations + 1, 2)
            where id = new.id;
    end if;

    return null;
end;
$$;

drop trigger if exists trg_reports_after_insert on public.reports;
create trigger trg_reports_after_insert
    after insert on public.reports
    for each row
    execute function public.reports_after_insert();

-- ---------------------------------------------------------------------------
-- AFTER INSERT on report_confirmations: bump the target report's
-- confirmations; confirm at >= 2.
-- ---------------------------------------------------------------------------
create or replace function public.report_confirmations_after_insert()
returns trigger
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
begin
    update public.reports
        set confirmations = confirmations + 1,
            status = case when confirmations + 1 >= 2 then 'confirmed' else status end
        where id = new.report_id
          and status <> 'expired';

    return null;
end;
$$;

drop trigger if exists trg_report_confirmations_after_insert on public.report_confirmations;
create trigger trg_report_confirmations_after_insert
    after insert on public.report_confirmations
    for each row
    execute function public.report_confirmations_after_insert();

-- ---------------------------------------------------------------------------
-- active_reports view (CLAUDE.md §5 / service/reports.py contract).
-- security_invoker = true: runs with the querying role's own RLS, which is
-- fine here since reports_select_all already permits anon/authenticated
-- select on the base table.
-- ---------------------------------------------------------------------------
create or replace view public.active_reports
with (security_invoker = true)
as
select
    r.id,
    r.type,
    extensions.ST_Y(r.location::extensions.geometry) as lat,
    extensions.ST_X(r.location::extensions.geometry) as lon,
    r.status,
    r.source,
    r.confirmations,
    r.created_at,
    r.expires_at
from public.reports r
where r.status = 'confirmed'
  and (r.expires_at is null or r.expires_at > now());

grant select on public.active_reports to anon, authenticated;
