# Way2Go

Accessibility-aware walking navigation for Cornell's central campus, built at Big Red Hacks 2026 (theme: Navigation).

Routes are personalized to each user's needs (stairs, slope, cross slope, curb cuts, lighting, accessible entrances) using Cornell Facilities GIS data, a Python routing service, and Supabase. Runs on localhost.

Status: in progress (hackathon build).

## Opening the project locally

```bash
git clone https://github.com/Janiciete/crusty-coders.git
cd crusty-coders
```

(The GitHub repo is still named `crusty-coders`; the product itself is Way2Go.) If you already have the project on your machine, just open a terminal and `cd` into the `crusty-coders` folder.

For setting up the Python environment, filling in `.env`, and running the pipeline/service, see the run commands in `CLAUDE.md` (§4).

- Project context, data contracts, and run commands: `CLAUDE.md`
- Full project plan: `docs/project_plan.md`
- API for the UI team: `docs/API.md`
- Way2Go UI (map-first app): `docs/UI.md`, `docs/DESIGN.md`

Routes are guidance, not official ADA certification. Slopes estimated from lidar are labeled as estimates.