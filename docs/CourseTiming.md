# Wind-based course time estimates

How long a course will take, for the median sailor, given the wind speed and the
course geometry (leg lengths, leg directions and mark order). The model is fitted
to tracker data from real races and refitted each time we race with trackers.

Page library: `WebUI/js/course-time.js` (used by `courses.html`). Fitting scripts:
`scripts/course_time/`. Current parameters: `scripts/course_time/params.json`.

## Why

The courses page used to estimate race time from course length alone, at one
speed per wind strength. It ignored leg direction, so a beat counted the same as
a reach, and it was far too fast in light wind: on the Parkdale races of
2026-11-15 it was 32% short in 9 kn, 20% short in 14 kn and about right in 21 kn.
That is the "old page" column below.

## The model

All angles are degrees true; TWA is the angle between a leg's bearing and the
direction the wind blows from (0 = dead upwind, 180 = dead downwind). TWS is the
wind speed in knots as measured by the wind station.

1. Upwind VMG (speed made good towards the wind, including tacks):
   `VMG = vmg_slope × (TWS + w0)` knots.
2. Each leg, distance D metres:
   - **Beat** (TWA ≤ 50°): tacked; time = D·cos(TWA) / VMG.
   - **Straight** (50° < TWA ≤ 165°): speed = VMG × ratio(TWA), where ratio is
     linearly interpolated from the table below.
   - **Gybed** (TWA > best downwind angle): the downwind mirror of tacking. Sail at
     the angle φ that maximises ratio(φ)·|cos φ| (the best downwind VMG), time =
     D·|cos TWA| / downwind VMG + one gybe. Used whenever it beats sailing straight;
     always used beyond 165°, which nobody sailed straight.
3. Each mark rounding adds `turn_s`, or `gybe_s` if both legs are downwind of a
   beam reach and on opposite gybes.
4. Course time = sum over legs from the middle of the start line, through the
   marks in order, to the finish (middle of the finish line if it has a pin),
   exactly as the courses page measures course length.

Current parameters (median sailor, fitted to 114 sailor-legs, 3 races):

| parameter | value | meaning |
|---|---|---|
| vmg_slope | 0.1650 | upwind VMG per knot of (TWS + w0) |
| w0_kn | 2 | light-wind offset (fixed, chosen by leave-one-race-out) |
| ratio @ 50° | 1.556 | = 1/cos 50°, joins the beat smoothly (not fitted) |
| ratio @ 90° | 3.057 | beam reach |
| ratio @ 135° | 2.440 | broad reach |
| ratio @ 165° | 2.064 | deepest straight run |
| best downwind VMG | 2.00 × VMG at 161° | derived |
| turn_s / gybe_s | 2 s / 6 s | fixed, from rounding speed losses |

As a fraction of TWS + 2 kn: close-hauled leg 0.26, beam reach 0.50, broad reach
0.40, deep run 0.34.

## Validation (Parkdale, 2026-11-15)

Target: median elapsed time (start-line crossing to finish) of the sailors who
completed the course. Predictions use the wind averaged over the median sailor's
race. LORO = fitted without that race. "1st leg" = wind direction assumed to blow
straight down the first leg instead of the measured direction.

| race | wind | km | actual | fit | LORO | LORO, 1st leg | old page |
|---|---|---|---|---|---|---|---|
| Race 1 | 8.8 kn | 3.62 | 37.9 | 38.8 | 39.3 (+4%) | 42.2 (+11%) | 27.9 |
| Race 2 | 14.2 kn | 3.45 | 26.9 | 25.8 | 25.4 (−6%) | 26.0 (−3%) | 21.5 |
| Race 3 | 20.8 kn | 3.42 | 17.7 | 18.3 | 18.4 (+4%) | 18.9 (+7%) | 17.1 |

The first-leg assumption makes the beats dead upwind (more tacking distance than
the 12–23° off-wind beats actually sailed), so it reads a few percent long here.

Sailors spread from 0.80 to 1.14 of the median sailor's speed.

## In the courses page

`WebUI/js/course-time.js` (`CourseTime`) implements the model with the fitted
parameters embedded in `PARAMS`; `test_js_parity.py` checks it matches the Python
and `params.json` exactly. `courses.html` shows `CourseTime.estimate(course, kn)`
for the wind slider speed, in the course list and the info box (per sub-course
for multi-course files).

- Legs come from `CourseOrder.sequence(course)`, the same points the course
  length is measured on.
- **Wind direction: the first leg is assumed to be a beat** (wind from the first
  leg's bearing). Right for windward/leeward, triangle and trapezoid courses;
  wrong for slalom courses, which start on a reach. `estimate(course, kn,
  windFrom)` takes an explicit direction for when the page has one (e.g. live
  wind, corrected by the station offset).
- After a refit, copy the numbers from `params.json` into `PARAMS` and run
  `test_js_parity.py`.
- Possible additions: flag legs at untested angles (TWA 50–80°, > 170°) and winds
  below 7 or above 22 kn.

Example predictions, first leg into the wind (minutes):

| course | km | 5 kn | 10 kn | 15 kn | 21.5 kn |
|---|---|---|---|---|---|
| Sunday 2026-11-15 | 3.74 | 67.6 | 39.6 | 28.1 | 20.4 |
| pyc Trangle#1 | 2.27 | 41.5 | 24.4 | 17.3 | 12.6 |
| Trapezoid | 2.77 | 51.6 | 30.3 | 21.5 | 15.6 |
| Trapezoid starboard | 2.77 | 51.5 | 30.2 | 21.4 | 15.6 |

The 5 kn column is an extrapolation (no data below 7 kn).

Not included: time from the gun to crossing the line (0–2 min at Parkdale,
estimated guns only) and pursuit-race start offsets.

## Known limitations

From our own checks and an independent Codex review:

- **One day, one venue, one fleet, three races, all on the same course shape.**
  Coefficients will move as data is added.
- **Untested angles.** Every beat was within 25° of dead upwind and every run
  157–168°. The close-reach part (50–80°) is a straight-line interpolation, and
  dead runs depend on the gybing assumption. Trapezoids have dead runs; wind
  shifts on triangles give close reaches.
- **Wind range 7–22 kn.** Speed is linear in wind; there is no explicit planing
  step. Race 3's beam reach was 26% faster than predicted.
- **Short legs.** The 2 s / 6 s turn costs are rough. They matter on slalom-length
  legs (≈ 4–11% of a 150–300 m-leg slalom) and are not yet calibrated.
- **Wind station.** Direction is corrected by `wind_offset_deg` per day (+7° at
  Parkdale, from the tack headings; anything from +2° to +13° fits). The station
  is on shore; the on-water wind differs.
- **Start/finish lines.** Saved per-race lines may not match where boats actually
  crossed; the config can give the as-laid finish line explicitly.

## Adding a race day

1. In the review page, mark each race as a sublog and save its course as laid
   (per-race course), with the marks where the buoys actually were.
2. Fetch the data (keep it outside the repo):

   ```
   scripts/course_time/fetch_race_data.py --event 11 --date 2026-11-15 --out DAYDIR
   ```

3. Edit `DAYDIR/config.json`, then save a copy as
   `scripts/course_time/days/<venue>_<date>.config.json`:

   ```json
   {
    "log_file": "2026_11_15.jsonl",
    "races": {"Race 1": {"gun": 1794708475.2, "pursuit": false},
              "Race 3": {"gun": null, "pursuit": true}},
    "mark_trackers": {"1": "G378830", "2": "G378848"},
    "finish_line": [{"tracker": "G312359"}, [-37.99768, 145.07152]],
    "wind_offset_deg": 7,
    "sailors": ["G623050", "Frank"],
    "exclude": {"Race 3": {"AndrewT": "did not round M2/M3"}}
   }
   ```

   - `gun`: unix time of the start signal (record it on the day if you can).
     Pursuit races use each sailor's own line crossing.
   - `mark_trackers`: tracker id on each mark, so moved marks are followed.
   - `finish_line`: `"course"` (use the course's finish line), or two ends, each
     `[lat, lon]` or `{"tracker": id}`.
   - `sailors`: tracker ids to use (default: every tracker with role sailor).
   - `exclude`: sailors to drop from a race, with the reason.
   - `"skip": true` on a race leaves it out.

4. Check detection, and the wind offset:

   ```
   scripts/course_time/course_time_model.py report DAYDIR
   scripts/course_time/course_time_model.py offset DAYDIR
   ```

5. Refit on all days and check:

   ```
   scripts/course_time/course_time_model.py fit DAY1 DAY2 ... -o scripts/course_time/params.json
   # copy the params.json numbers into PARAMS in WebUI/js/course-time.js, then
   scripts/course_time/test_js_parity.py scripts/course_time/params.json DAYDIR/courses.json
   scripts/course_time/course_time_model.py predict --params scripts/course_time/params.json --courses DAYDIR/courses.json
   ```

## Data that would help most

1. Recorded gun times, and buoy trackers on every mark.
2. A trapezoid day (dead runs) and a triangle in a shifted wind (close reaches).
3. Short and long reaches in similar wind, to measure gybe and rounding costs.
4. A day under 7 kn and a day over 20 kn.
5. A different venue or fleet.
