0. Project at a Glance
Item
Decision
What it is
A navigation web app that routes people based on their individual accessibility needs, not just distance or time
Where it works
Cornell central campus (demo zone), designed to expand
Core data
Cornell Facilities GIS: sidewalk inventory, crosswalks, entrances, stairs, lighting, buildings
Routing
Custom graph + personalized cost function + Dijkstra/A*, written in Python
Backend and database
Supabase for authentication, database (Postgres + PostGIS), live updates (Realtime), and file storage
Hosting
Everything runs on localhost. No AWS or Azure.
AI
xAI API ($25 credit) for optional in-app AI features. Cursor credits are for coding assistance only (see Section 9).
Key novelty
Routing on cross slope and surveyed surface data, standards-based route explanations, weather- and darkness-aware costs, a bottleneck finder for campus facilities, and one-tap live reports triggered by detected slowdowns


1. Problem and Vision
1.1 Problem
Current navigation systems optimize for time, distance, and transportation mode, not for an individual's accessibility needs.
The shortest route may be unusable or much harder for someone with a disability.
Accessibility is personal: a route that works for one person may not work for another.
Cornell's campus is especially difficult: steep hills (Libe Slope), many staircases, winter ice, and dark paths at night.
1.2 Goal
Create a navigation system where the user defines what an accessible route means to them. Accessibility becomes a personalized, continuously updated routing problem instead of a static label such as "wheelchair accessible."
1.3 100-Year Vision
Accessibility-aware navigation becomes the standard way people move through physical spaces.
Accessibility is built into navigation itself, rather than layered on top of traditional maps as a separate tool.
The long-term goal is a system that understands both the physical environment and each person's needs well enough to provide personalized navigation anywhere.
1.4 Pitch Statistic
In central campus alone, 209 path segments exceed the 5% ADA walkway slope limit and 73 exceed the 8.33% ADA ramp limit. Standard map apps do not know which ones.

2. Users and Stakeholders
2.1 Primary Users
User
Main needs
Wheelchair and mobility-aid users
No stairs, gentle slopes, low cross slope, adequate width, smooth surfaces, curb cuts, accessible entrances
People with temporary injuries (crutches, casts)
No stairs, gentle slopes, handrails, shorter distances
Blind and low-vision users
Lit paths, fewer road crossings, fewer surface defects
People with limited endurance
Gentler slopes, shorter distances
Anyone walking alone at night
Well-lit paths
Students, faculty, staff, and visitors
Convenience: fastest route, weather-aware routing

2.2 Stakeholders
Stakeholder
Value to them
Cornell Facilities
Bottleneck finder ranks which segments to repair first; aggregated reports show recurring problems
Student Disability Services
Reliable route planning for students with accommodations
Campus Safety
Lighting gaps identified along common night routes
Admissions and visitor services
Accessible campus tours


3. Functional Scope
3.1 How Users Express Their Needs (functional, not visual)
Profiles (saved): Wheelchair, Injured / Limited Mobility, Low Vision, Night Walk, Fastest. Profiles can be combined (for example, Injured + Low Vision).
Right-now adjustments: in a hurry, carrying items, walking alone.
Automatic conditions (no user action):
Darkness: turns on after sunset (computed with the suncalc library).
Ice/snow: turns on when the weather forecast shows freezing temperatures or snow.
Users can turn off any automatic condition.
Stretch: natural-language input. AI converts a sentence such as "Hills are really hard for me, but I don't mind walking farther" into the same profile settings. AI never performs the routing.
3.2 Accessibility Features
Core (built, real data)
Feature
Data source
Default rule
Stairs and step counts
Walk Inventory (travel = Stair) + Stairs layer
Blocked for Wheelchair and Injured profiles
Slope (running gradient)
Walk Inventory slope_1; USGS lidar for gaps
ADA: 5% walkway limit; 8.33% ramp limit
Cross slope
Walk Inventory crossslo_1 + Crosswalks
ADA: 2% maximum
Sidewalk width
Walk Inventory width
ADA: 36 in (3 ft) minimum clear width
Surface quality
Walk Inventory material_1 + defect fields
Penalize cobblestone, gravel, and high-severity defects
Ramps
Walk Inventory ramp_1
Present only where marked
Curb cuts
Crosswalks curb_cut (0, 1, or 2 per crosswalk)
Crosswalks with fewer than 2 curb cuts are blocked for wheelchair users
Lighting
Campus Lighting (fixture density per segment)
Penalize dark segments for Night Walk and Low Vision, or whenever it is dark
Accessible entrances
Entrance Inventory 2025
Routes end at an accessible entrance; unknown doors are treated as unverified

Novel, low-effort features (built)
Feature
Description
Conditional costs
Freezing weather multiplies slope penalties; darkness activates lighting penalties. Routes change with conditions, not only with the user.
Standards-based explanations
Each route names the exact segment and standard it avoids or fails, for example: "Libe Slope segment: 11% slope, exceeds the 5% walkway limit."
Bottleneck finder
Runs the Wheelchair profile across many entrance-to-entrance trips and ranks the segments that block the most routes: "Fixing these 5 segments makes X% more of campus reachable."
Passive slowdown detection
Detects when a user slows sharply, stops, or leaves the route, then asks one quick question (Section 6).

Stretch (only after the core is complete)
Crowds: estimated from class-change times near large lecture halls, labeled "estimated."
Road-crossing count per route.
Natural-language preference input (xAI).
Removed from scope
Elevators, seating/rest stops, and emergency phones (no reliable campus data; removed to keep scope focused).

4. Data Sources (all verified as accessible)
4.1 Cornell Facilities GIS
Public, no API key required.
Base URL: https://gis.fcs.cornell.edu/arcgis/rest/services/Production/
Exports as GeoJSON in latitude/longitude (EPSG:4326), with pagination (2,000 records per request).
Our scripts only run read-only queries.
Layer
Service path
Contents
Verified facts
Walk Inventory 2026
WalkInventory_2026/FeatureServer/0
Sidewalk centerlines
5,689 segments (~141,000 ft). Demo zone: 2,341 segments, 70% of length surveyed, slope filled on 99% of surveyed segments. Fields: travel type, slope (%), cross slope (%), width (ft), material, ramp, handrail, cracking/defects, survey date.
Crosswalks
Path_of_Travel_2022/FeatureServer/1
Road crossings
288 crosswalks (119 in demo zone), all with slope, cross slope, condition, striping. Curb cuts: 252 have 2, 19 have 1, 16 have none.
Entrance Inventory 2025
EntranceInventory_2025/FeatureServer/0
Building doors
2,978 doors. Automatic opener: 245 yes. Accessible path of travel: 993 yes. Threshold (under ½ in, beveled), clear opening width, access control (open/card/key), door type.
Stairs
Stairs_Polygon/FeatureServer/0
Staircase footprints
779 staircases; step count on 741; landings, handrail side, condition. Updated 2018/2022.
Campus Lighting
Campus_Lighting/FeatureServer/17
Light fixtures
6,679 fixtures. Type 99% filled, height 94%, wattage 47%, lumens only 2% (so lighting is scored by fixture density). Partially updated 2019.
Buildings
Buildings/FeatureServer/0
Building footprints
1,220 buildings with names (destination search, matching doors to buildings).
Trip Hazards
Trip_Hazards/FeatureServer/0
Historical hazard entries
15 undated entries; used only as labeled demo seed reports.

Map viewer links:
Walk Inventory: https://www.arcgis.com/apps/mapviewer/index.html?url=https://gis.fcs.cornell.edu/arcgis/rest/services/Production/WalkInventory_2026/FeatureServer/0&source=sd
Campus Lighting: https://www.arcgis.com/apps/mapviewer/index.html?url=https://gis.fcs.cornell.edu/arcgis/rest/services/Production/Campus_Lighting/FeatureServer/17&source=sd
4.2 Other Sources
Source
Purpose
Verified result
Notes
National Weather Service API (api.weather.gov)
Hourly temperature and conditions for ice/snow rules
Campus maps to forecast grid BGM 45,70
Free, no key; requires a User-Agent header
USGS Elevation Point Query Service (epqs.nationalmap.gov/v1/json)
Elevation to estimate slope on unsurveyed segments
252.3 m at central campus from 1 m lidar (2020)
Free, no key; one point per request; results cached
OpenStreetMap via Overpass API (overpass-api.de)
Backup walking network and crossings
Demo zone: 590 footways/paths, 101 crossings, 66 stairways
Free; use POST requests
suncalc (JavaScript library)
Sunset/sunrise times for darkness rules
Available on npm
Computed locally, no API
User reports
Live layer
Stored in Supabase
—

4.3 Feature-to-Data Map
Feature
Source
Stairs and step counts
Walk Inventory + Stairs
Slope
Walk Inventory; USGS lidar for gaps
Cross slope
Walk Inventory + Crosswalks
Width, surface, defects, ramps, handrails
Walk Inventory
Curb cuts
Crosswalks
Accessible entrances
Entrance Inventory 2025
Lighting
Campus Lighting
Ice and darkness
NWS API + suncalc
Destinations
Buildings
Live conditions
Supabase reports

4.4 Demo Zone
Central campus bounding box (longitude/latitude): west −76.4905, south 42.4430, east −76.4790, north 42.4520. Covers West Campus, Libe Slope, the Arts Quad, Ho Plaza, and the Engineering Quad.

5. Data Pipeline
5.1 Scripts (already written)
Script
Purpose
Output
fetch_cornell_data.py
Downloads all Cornell layers (no installs needed)
cornell_data/*.geojson and *.csv
build_graph.py
Cleans data, fixes known issues, builds the routing graph
cornell_data/graph/: edges.geojson, nodes.geojson, graph.pkl, seed_reports.geojson, build_report.txt

Run order: fetch script, then build_graph.py --elevation --bbox -76.4905 42.4430 -76.4790 42.4520. Requires geopandas, networkx, and shapely. Note: build_graph.py was tested on synthetic data that matches Cornell's schema; the first real run may need minor adjustments.
5.2 Data Issues and Resolutions (tested on real demo-zone data)
Issue
Resolution
Result
Disconnected sidewalk network
Add crosswalks, snap endpoints within 10 ft (splitting lines at touch points), then bridge remaining gaps up to 35 ft as flagged "inferred" edges
Largest connected piece: 30% → 90% (snapping + crosswalks) → 97% (gap bridging)
Inconsistent travel labels
Normalized to: preferred, preferred_candidate, stair, steep, service_route, other, unlabeled, needs_check
Resolved
51% slope outlier
A single 21-ft segment, 53 ft from any staircase; flagged as an error and re-measured from lidar
Resolved
~30% of demo-zone length unsurveyed
~17,550 ft unsurveyed, of which 2,541 ft are stairs (slope irrelevant). The rest is filled from USGS 1 m lidar and labeled "estimate." Segments under 20 ft remain "unknown."
Resolved
Blank entrance fields
Blanks are treated as "unknown," never "accessible"; exit-only and sealed doors removed
354 accessible entrances in the demo zone; 228 within 10 ft of a path, 347 within 100 ft; approaches over 50 ft flagged
Stairs stored as polygons
Stair segments matched to stair polygons
370 of 404 stair segments matched; 359 have step counts
Unlabeled stairs (safety)
107 non-stair segments lie on staircase footprints; re-tagged as stairs
Wheelchair routes can never use an unlabeled staircase
Dates stored in milliseconds
Converted to YYYY-MM-DD
Resolved


6. System Architecture (localhost)
6.1 Components
Component
Runs where
Responsibility
Data pipeline (Python)
Developer laptop, run once
Produces the cleaned graph
Routing service (Python)
localhost
Loads graph.pkl, applies the user's profile and live reports, computes and explains routes
Supabase
Supabase cloud (accessed from localhost)
Authentication, user data, reports, live updates, optional photo storage
Weather and sunset checks
Routing service / client
NWS API and suncalc set automatic conditions
AI (optional)
Routing service
xAI API converts natural-language needs into profile settings
Front end
localhost
Not planned yet (UI/UX excluded)

6.2 Data Flow for One Route Request
The user's device gets their location (browser Geolocation API) and sends the start point, destination, and active profile to the local routing service.
The routing service reads active, confirmed reports from Supabase and closes or penalizes affected edges.
It checks automatic conditions (weather, darkness).
It runs Dijkstra/A* with the personalized cost function and returns several route alternatives with statistics.
It generates a plain-language explanation for each route from those statistics.
Location is used for the calculation and then discarded. It is never stored.
6.3 Localhost Constraint
Browsers allow precise location on http://localhost because localhost counts as a secure context.
A phone connecting to the laptop over the local network (for example, http://192.168.x.x) is not a secure context, so location will be blocked there.
Demo plan: run the demo in a browser on the laptop, and use a replayed GPS trace to simulate walking (this also demonstrates passive slowdown detection).

7. Supabase (all scopes)
7.1 Services Used
Supabase service
Use
Auth
Optional login (email magic link or Google). Guest mode works without an account.
Postgres database
Profiles, saved places, reports, confirmations
PostGIS extension
Spatial queries, such as finding reports within 20 m of a route or edge
Row Level Security (RLS)
Each user can read and write only their own profile and saved places
Realtime
Pushes new and confirmed reports to every open session so routes update immediately
Storage
Optional photos attached to reports
pgRouting extension (optional)
Supported by Supabase; a fallback if routing ever needs to move into the database. Not part of the main plan.

7.2 Tables
Table
Columns (planned)
Access rule
profiles
user_id, preferences (JSON: avoid_stairs, max_slope_pct, max_cross_slope_pct, min_width_ft, prefer_lit, surface_sensitivity, distance_tolerance), created_at, updated_at
Owner only (RLS). Stores routing preferences, never disability labels or medical information.
saved_places
id, user_id, name, location (PostGIS point)
Owner only (RLS)
reports
id, type, location (PostGIS point, rounded to ~10 m), note (optional), photo_path (optional), created_at, expires_at, confirmations, status (pending, confirmed, expired), source (user, seed)
Public read; insert by anyone; no user ID stored
report_confirmations
report_id, user_hash (one-way hash, prevents duplicate votes), created_at
Insert only; raw user ID never stored

7.3 Guest Mode
Routing and reporting work without an account; preferences are kept on the device.
Logging in only syncs preferences and saved places across devices.
Build guest mode first so the demo never depends on login.
7.4 Key Handling
The Supabase public (anon) key may be used in the front end only with RLS enabled on every table.
The Supabase service role key stays in the local routing service and is never sent to the browser.

8. Routing Engine
8.1 Graph Model
Nodes: path intersections, crosswalk ends, and building entrances.
Edges: sidewalk segments, crosswalks, snap connectors, door approaches, and flagged inferred gaps.
Edge attributes (from build_graph.py): length_ft, kind, travel, is_stairs, steps, landings, rail_side, slope_pct, slope_source (cornell_survey or usgs_lidar_1m_estimate), cross_slope_pct, width_ft, surface, defect_level (0–3), ramp, handrail, curb_cuts, surveyed, date_surveyed, lit_fixtures, lit_per_100ft, lit_watts, inferred, verified, flags.
Entrance node attributes: door_id, access (accessible, not_accessible, unknown), auto_opener, threshold_ok, clear_width_in, access_control, building.
8.2 Cost Function
Each profile defines hard limits (edges removed) and soft penalties (edges made more expensive):
edge cost = length × (1 + slope penalty + cross-slope penalty + surface penalty + lighting penalty + unverified penalty) + condition multipliers
8.3 Profile Defaults (tunable)
Profile
Hard limits
Soft penalties
Wheelchair
No stairs; no slope above 8.33%; slope 5–8.33% allowed only where ramp = Yes; crosswalks need 2 curb cuts; width at least 3 ft; destination must be an accessible entrance
Cross slope above 2%; rough surfaces and defects; inferred/unverified edges
Injured / Limited Mobility
No stairs
Slope above 5%; distance; missing handrails on steep segments
Low Vision
None
Dark segments; surface defects; crossings without full curb cuts
Night Walk
None
Dark segments (strong); inferred/unverified edges
Fastest
None
Distance only

Fallback rule: if no route satisfies the hard limits, relax them into heavy penalties, return the best available route, and clearly warn which limits it breaks.
8.4 Automatic Condition Rules
Condition
Trigger
Effect
Ice/snow
NWS hourly forecast at or below 34°F, or snow/ice in the forecast
Slope penalties multiplied; steep and unverified segments penalized further
Darkness
Current time between sunset and sunrise (suncalc)
Lighting penalty activated for all profiles
Live reports
Confirmed report within ~20 m of an edge
Blocked path/construction: edge removed. Ice/too steep/too dark: edge penalized.

8.5 Route Output
For each alternative: total distance and estimated time, steps avoided, maximum slope, maximum cross slope, percentage lit, entrance accessibility at the destination, reports avoided, number of unverified segments, and the plain-language explanation.
8.6 Explanations (template-based, no AI needed)
Example:
Why this route?
Avoids 3 staircases (38 steps)
Maximum slope: 4.2% (within the 5% limit)
Maximum cross slope: 1.8% (within the 2% limit)
Ends at an accessible entrance with an automatic door
92% of the route is lit
4 minutes longer than the shortest route
Avoids a reported blocked path
Trust features: verified (surveyed) data is distinguished from estimated data, each segment shows its survey date, and users can change preferences and immediately see how the route changes.
8.7 Bottleneck Finder
Select many origin–destination pairs between accessible entrances in the demo zone.
For each pair, compute the shortest unconstrained route and the Wheelchair-profile route.
When the Wheelchair route is missing or much longer, record which edges on the unconstrained route violate a standard (stairs, slope, cross slope, missing curb cuts).
Rank edges by how many trips they block.
Output: the top segments, the standard each violates, and the percentage of trips that would become accessible if each were fixed.

9. AI Plan
Credit
What it can be used for
Notes
Cursor
Coding assistance inside the Cursor editor only
Cursor credits work only within the editor and cannot be used as an API key for our app
xAI API ($25)
In-app AI features
Plenty for short requests that return small JSON objects

In-app AI uses (all optional; the app fully works without AI):
Natural-language preferences (stretch): convert a sentence into profile settings (JSON). The output is validated against allowed values before use.
Report note classification (stretch): convert an optional free-text note into a report type.
AI never computes routes. Routing is always done by the graph algorithm so results are reliable and explainable.

10. Live Layer
10.1 Report Types
Blocked path, ice, construction, too steep, too dark, too loud, crowded. The slowdown prompt also includes "Nothing, just me."
10.2 Passive Slowdown Detection
During navigation, the app compares the user's pace and position with the expected time for each edge (based on the profile's walking speed).
A spot is flagged when the user moves much slower than expected, stops for an extended period, or leaves the suggested route.
Detection runs on the user's device. Only the flagged spot is sent, and only if the user answers the prompt.
10.3 The Prompt
Shown at a natural pause (standing still or arriving), never while moving.
One question: "What slowed you down?" with one-tap answers.
Under three seconds to answer; a photo or note is optional; dismissible with a swipe.
Rate-limited (for example, at most one prompt per trip and a few per day) so it never feels like a chore.
10.4 Confirmation and Expiry (defaults, tunable)
A report becomes confirmed when at least 2 independent reports of the same type occur within ~15 m and a short time window.
Expiry by type: ice ~6 hours, blocked path ~24 hours, construction ~7 days, too dark/too loud/crowded ~2 hours.
Confirmed reports reroute everyone affected through Supabase Realtime.
Recurring patterns feed the bottleneck finder as evidence for Facilities.
10.5 Demo Seed Data
Cornell's 15 undated trip-hazard entries are loaded as seed reports, clearly labeled as historical rather than live.

11. Privacy and Ethics
11.1 Design Principle
The system should know what the user needs for navigation without needing to know who the user is.
11.2 Privacy Rules
Never ask users to identify a disability; store routing preferences, not medical information.
Location is requested only when navigation starts, with a one-sentence explanation.
Location is used per request and never stored; no location history exists in the database.
Passive detection keeps only flagged spots, never full traces.
Reports are anonymous and rounded to ~10 m.
Guest mode allows fully anonymous use.
Row Level Security on every table; the service role key never reaches the browser.
Users can delete their account, which removes their profile and saved places.
Individual profiles are never shared with third parties; Facilities receives only aggregated data.
11.3 Ethics
No crime-based routing: it stigmatizes places and gives false confidence. Night routing uses lighting instead, and the feature is called "well-lit route," not "safe route."
Routes are guidance, not official ADA certification.
Report confirmation and expiry prevent false or stale reports from dominating routes.
Estimated data (lidar slopes, inferred connections, crowds) is always labeled as estimated.

12. Build Priorities
Tier
Items
Must (the demo)
Data pipeline run on real data; routing service with all five profiles; hard limits, penalties, and fallback rule; accessible-entrance destinations; route explanations; Supabase reports with Realtime rerouting; guest mode
Should (polish)
Weather and darkness conditions; bottleneck finder; Supabase Auth with saved profiles and places; seed reports
Stretch
Simulated passive slowdown detection (GPS replay); natural-language preferences via xAI; crowd estimates; road-crossing counts; report photos in Storage
Vision (pitch only)
Indoor navigation with elevators; voice and haptic navigation; computer vision detection of sidewalk problems; transit integration; expansion to other campuses and cities


13. Timeline (Saturday night to Sunday morning)
Time
Work
9:45–10:30 PM
Run fetch_cornell_data.py and build_graph.py; review build_report.txt; create the Supabase project, enable PostGIS, create tables and RLS policies
10:30 PM–12:30 AM
Routing service: profiles, cost function, fallback rule, route statistics; verify routes in the demo zone match reality
12:30–2:00 AM
Live layer: reports table, report-to-edge matching, Realtime rerouting; weather and darkness conditions
2:00–3:30 AM
Explanations; bottleneck finder; Supabase Auth with saved profiles (on top of guest mode)
3:30–5:00 AM
Stretch: GPS-replay passive detection; xAI natural-language preferences
5:00–6:30 AM
End-to-end testing; verify the three demo routes; Devpost write-up
6:30–8:00 AM
Rehearse the demo; record a backup video; buffer for fixes
8:30 AM
Judging

UI/UX work runs in parallel and will be planned separately.

14. Demo and Pitch
14.1 Demo Script (about 90 seconds)
Hero story: a student on crutches needs to get from West Campus to the Arts Quad for an 8:40 AM class in the snow.
Same trip, three profiles (Fastest, Injured, Wheelchair): three different routes, each with its "Why this route?" explanation.
Live report: a "blocked path" report is confirmed, and the Wheelchair route reroutes instantly.
Conditions: switch to night or freezing weather and watch the route change.
Facilities angle: the bottleneck finder shows the top segments that block wheelchair access.
Close: the 100-year vision, navigation that adapts to the person.
14.2 Judging Criteria Coverage
Criterion
How we address it
Technical skill
Real GIS data pipeline, network repair (30% → 97% connected), multi-criteria routing, PostGIS and Realtime
Design
Clear explanations and trust indicators (UI planned separately)
Creativity
Cross slope routing, conditional costs, bottleneck finder, slowdown-triggered reports
Impact
Real ADA violations on Cornell's campus; value to students and Facilities
Theme
Navigation personalized to the person, with a clear 100-year vision


15. Risks and Mitigations
Risk
Mitigation
Real data breaks build_graph.py
Run it first thing; fall back to the OpenStreetMap network with Cornell attributes attached
No route exists under strict limits
Fallback rule relaxes limits and warns the user
Supabase or network outage during the demo
Guest mode; routing works offline from graph.pkl; seed reports cached locally
Location blocked outside localhost
Demo on the laptop; GPS replay for movement
Wrong data on a demo route
Verify all three demo routes against real conditions before judging
xAI unavailable or credits run out
AI features are optional; profiles work without AI
Time overrun
Follow the Must → Should → Stretch order strictly


16. Out of Scope (and why)
Feature
Reason
Elevators, seating/rest stops, emergency phones
No reliable campus data; removed to keep scope focused
Crime rates
No segment-level campus data; ethical concerns
Air quality
Does not vary meaningfully between paths on one campus
Shade/sun exposure
Requires a canopy and sun model; low payoff
Noise as routing data
No data; kept as a report type only
Dangerous intersections
Requires crash data; most routes are campus paths
Bus delays and transit
Requires transit routing
Cloud hosting (AWS/Azure)
The project runs entirely on localhost
EMCS energy dashboard
Building energy data, not relevant to accessibility


17. Future Work
Expand from Cornell to other universities, then cities, airports, hospitals, and transit systems.
Indoor navigation, including elevators.
Seating and rest-stop-aware routing.
Voice-based navigation for blind and low-vision users.
Haptic and audio wearable integration.
Computer vision to detect accessibility problems missing from existing datasets.
Public transportation integration.
Learning from a user's past routes and preferences.
Predicting crowding, noise, and construction from historical data.
Supporting additional disabilities and accessibility needs.
