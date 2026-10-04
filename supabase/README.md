# Supabase setup

Source of truth: `docs/project_plan.md` §7, §10.4, §11; binding details in `CLAUDE.md` §5–§7.

## Apply the migrations

In the Supabase dashboard, open **SQL Editor** and run the four files **in this
exact order**, each as its own query (paste the whole file, click Run, confirm
no errors, then move to the next):

1. `migrations/0001_tables.sql` — enables PostGIS and creates `profiles`,
   `saved_places`, `reports`, `report_confirmations` with their check
   constraints.
2. `migrations/0002_rls.sql` — enables Row Level Security on all four tables
   and adds the owner-only / public-read / insert-only policies.
3. `migrations/0003_triggers.sql` — adds the location-rounding, expiry, and
   confirmation triggers, plus the `active_reports` view.
4. `migrations/0004_realtime_and_account.sql` — adds `public.reports` to the
   `supabase_realtime` publication and creates `delete_my_data()`.

## Verify in the dashboard (human checkpoint)

- **Table Editor → profiles / saved_places / reports / report_confirmations**:
  each should show an "RLS enabled" badge.
- **Database → Replication → supabase_realtime**: `reports` should be listed
  as a published table.
- Insert a throwaway row into `reports` via the SQL editor (as the `postgres`
  role) and confirm `active_reports` (Table Editor → Views) reflects it once
  `status = 'confirmed'` and `expires_at` is null or in the future.

## Run the integration test

```bash
source .venv/bin/activate
cp .env.example .env   # fill in SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY
pytest -q tests/test_supabase_integration.py
```

With `.env` empty or missing any of the three keys, this test file is
skipped (this is expected before the Supabase project exists). Once `.env`
is filled in and the four migrations have been applied, it should run and
pass, and it cleans up every row it creates (tagged `note = "TEST"`) with
the service-role key even if an assertion fails partway through.

Never commit `.env` or print a key. `SUPABASE_SERVICE_ROLE_KEY` is only ever
used from this test file and from the local routing service — never from
frontend code.
