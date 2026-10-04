-- 0002_rls.sql
-- Row Level Security for all four tables. Paste after 0001_tables.sql.

-- ---------------------------------------------------------------------------
-- profiles: owner only
-- ---------------------------------------------------------------------------
alter table public.profiles enable row level security;

create policy "profiles_select_own"
    on public.profiles for select
    to authenticated
    using (auth.uid() = user_id);

create policy "profiles_insert_own"
    on public.profiles for insert
    to authenticated
    with check (auth.uid() = user_id);

create policy "profiles_update_own"
    on public.profiles for update
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

create policy "profiles_delete_own"
    on public.profiles for delete
    to authenticated
    using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- saved_places: owner only
-- ---------------------------------------------------------------------------
alter table public.saved_places enable row level security;

create policy "saved_places_select_own"
    on public.saved_places for select
    to authenticated
    using (auth.uid() = user_id);

create policy "saved_places_insert_own"
    on public.saved_places for insert
    to authenticated
    with check (auth.uid() = user_id);

create policy "saved_places_update_own"
    on public.saved_places for update
    to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

create policy "saved_places_delete_own"
    on public.saved_places for delete
    to authenticated
    using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- reports: public read; anon/authenticated insert of user-sourced reports only;
-- no client update or delete (server-side triggers do all mutation).
-- ---------------------------------------------------------------------------
alter table public.reports enable row level security;

create policy "reports_select_all"
    on public.reports for select
    to anon, authenticated
    using (true);

create policy "reports_insert_user_source"
    on public.reports for insert
    to anon, authenticated
    with check (source = 'user');

-- Intentionally no update/update-via-trigger-only and no delete policy for
-- anon/authenticated: with RLS enabled and no matching policy, those commands
-- are denied by default for those roles. Service role bypasses RLS entirely
-- (used by seed loader and by the triggers' SECURITY DEFINER functions).

-- ---------------------------------------------------------------------------
-- report_confirmations: insert only; no select/update/delete for clients.
-- ---------------------------------------------------------------------------
alter table public.report_confirmations enable row level security;

create policy "report_confirmations_insert_only"
    on public.report_confirmations for insert
    to anon, authenticated
    with check (true);

-- No select/update/delete policy: denied by default for anon/authenticated
-- once RLS is enabled.
