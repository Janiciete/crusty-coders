-- 0004_realtime_and_account.sql
-- Realtime publication for reports, and account-deletion RPC.
-- Paste after 0003_triggers.sql.

-- ---------------------------------------------------------------------------
-- Realtime: push new/confirmed reports to subscribed clients.
-- [VERIFY] the `supabase_realtime` publication already exists on a fresh
-- Supabase project (it does by default); this statement is a no-op if
-- public.reports is already a member.
-- ---------------------------------------------------------------------------
do $$
begin
    if not exists (
        select 1
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'reports'
    ) then
        alter publication supabase_realtime add table public.reports;
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- delete_my_data(): removes the caller's profile and saved places.
-- Does NOT delete the auth.users row itself (requires the Auth admin API,
-- see CLAUDE.md §9 known issues).
-- ---------------------------------------------------------------------------
create or replace function public.delete_my_data()
returns void
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
declare
    v_uid uuid := auth.uid();
begin
    if v_uid is null then
        raise exception 'delete_my_data() requires an authenticated caller';
    end if;

    delete from public.saved_places where user_id = v_uid;
    delete from public.profiles where user_id = v_uid;
end;
$$;

revoke all on function public.delete_my_data() from public;
grant execute on function public.delete_my_data() to authenticated;
