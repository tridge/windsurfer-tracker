# GT06 table-driven settings reconciler — design

Goal: replace the imperative command builders (`_active_cmds`/`_idle_cmds`/
`_overnight_cmds`) and the scattered cxzt#-handler push logic with a **declarative
desired-state reconciler**. We describe, in tables, what every setting should be
in each state; on connect (and on every state change) we read the device's
current settings, diff against the desired table, and send only the commands
needed to fix the differences. Less ad-hoc code; correctness driven by data.

This also closes the coverage gaps found in [`gt06_settings.md`](gt06_settings.md)
(settings set in some states but not others) and the reconnect bug (re-toggling
`GPS_RST_TIME`/`SENDS` on every login) — both fall out for free once we only send
what's actually wrong.

## Three pieces

### 1. Setting descriptors (one row per controllable setting)

```python
@dataclass
class Setting:
    key: str                      # canonical name, e.g. "GPS_RST_TIME"
    set_cmd: Callable[[Any], str] # value -> wire command, e.g. lambda v: f"SZCS#GPS_RST_TIME={v}"
    readable_via: str | None      # "cxzt" | "param" | "query" | None (fire-and-forget)
    query_cmd: str | None         # individual query, e.g. "CXCS#GPS_RST_TIME"
    parse: Callable[[str], Any]   # response text -> normalized value
    norm: Callable[[Any], Any]    # normalize a desired/observed value for ==
    one_shot: bool = False        # MODE: send once; re-sending churns the device
    causes_reconnect: bool = False# MODE1 tears down TCP -> expect a reconnect
```

Key point we discovered: **readback is fragmented** — there is no single dump.
- `cxzt#` reports `M` (MODE), `F` (TIMER), `H` (HBT) + device info.
- `PARAM#` reports `TIMER`, `SENDS`, `HBT`, `Defense` (+ IMEI) — adds **SENDS**.
- Everything else (`SLPDISCONNECT`, `GPS_RST_TIME`, `VIBCHK`, `ACCLINE`,
  `SENALM`, `MOVING`) is only readable via its own `CXCS#<p>` / `<NAME>#` query.

So each descriptor records *how* its current value is observed. Settings with
`readable_via=None`/`query` only are the expensive ones.

### 2. Desired-state table (state × firmware → values)

```python
# value of None = "don't set in this state" (MODE governs it, e.g. TIMER in V667 sleep)
DESIRED = {
  "tracking": dict(MODE=1, TIMER="$active",  HBT=15, SENDS=0, SLPDISCONNECT=0,
                   GPS_RST_TIME=300, VIBCHK="0:16", ACCLINE=0, SENALM="OFF", MOVING="OFF"),
  "idle":     dict(MODE=1, TIMER="$idle_loc",HBT="$idle_hbt", SENDS=1, SLPDISCONNECT=0,
                   GPS_RST_TIME=0,   VIBCHK="0:16", ACCLINE=0, SENALM="OFF", MOVING="OFF"),
  "sleep":    {  # firmware-version-dependent (all units are W07C hardware)
     "V667":  dict(MODE=("MODE4","$ovn_sec"), TIMER=None, HBT=None, SENDS=0, SLPDISCONNECT=0,
                  GPS_RST_TIME=0, VIBCHK="0:16", ACCLINE=1, SENALM="OFF", MOVING="OFF"),
     "V6.6x": dict(MODE=1, TIMER="$ovn_loc", HBT="$ovn_loc", SENDS=1, SLPDISCONNECT=0,
                  GPS_RST_TIME=0, VIBCHK="0:16", ACCLINE=1, SENALM="OFF", MOVING="OFF"),
  },
}
```

`$active`/`$idle_loc`/`$idle_hbt`/`$ovn_sec`/`$ovn_loc` are resolved from config
(`interval`, `idle_loc_interval`, `idle_hbt_interval`, `overnight_interval_min`×60
via `_overnight_arg`). Values marked **TBD** below need vendor confirmation before
flipping from "not currently sent" to a concrete value (ACCLINE in day states;
SENDS/GPS_RST in V667 sleep) — until then the table documents the gap explicitly.

### 3. Believed/observed state (per device, persisted)

Extend the existing `device_state[imei]` (already persisted via
`_save_device_state`) with `believed = {key: value}`. Updated whenever:
- a query response is parsed (authoritative), or
- a `SETOK: <K>=<v>` ACK confirms a set we sent.

The firmware **persists settings across reboots** (that's why MODE1 is one-shot
and SLPDISCONNECT is "no-op if already set"), so believed-state is reliable for
the unreadable settings between connects.

## Reconcile algorithm

```
on_connect(conn):
    state    = determine_state(conn)            # existing precedence logic (active/idle/overnight)
    fwclass  = firmware_class(conn.firmware)     # V667 | V6.6x  (all W07C hardware)
    desired  = resolve(DESIRED, state, fwclass)  # {key: concrete value}, drop None
    observed = load_believed(conn.imei)          # persisted believed-state (may be empty)
    queue_queries(conn, queries_for(desired, depth))   # cxzt# always; PARAM# if any param-only key; CXCS#/NAME# per policy
    # ...responses stream in, each updates observed via descriptor.parse...
    on_queries_done(conn):
        apply_diff(conn, desired, observed)

on_state_change(conn, new_state):               # commanded (admin) or scheduled (evening sleep)
    desired = resolve(DESIRED, new_state, fwclass)
    apply_diff(conn, desired, conn.believed)     # diff vs believed; no re-query needed

apply_diff(conn, desired, observed):
    for key, want in desired.items():
        s = SETTINGS[key]
        if s.norm(observed.get(key)) == s.norm(want):
            continue                              # already correct -> send nothing
        if s.one_shot and key=="MODE":
            queue_mode_change(conn, want)         # one-shot; expect reconnect; storm-guarded
        else:
            queue_set(conn, s.set_cmd(want))      # SETOK updates believed[key]=want
```

Properties this gives us, by construction:
- **No churn**: a setting already at the desired value is never re-sent → fixes
  the reconnect bug (GPS_RST_TIME/SENDS no longer re-toggled on every login).
- **Full coverage**: every key in the desired dict is enforced in every state →
  no stale-value leakage between states.
- **State changes are cheap**: diff vs believed-state, send only the delta.

## Per-setting descriptor table (the data)

| key | set | readable_via | query | parse (current) | one-shot / reconnect |
|---|---|---|---|---|---|
| MODE | `MODE{n},{arg}#` | cxzt | — | `M:(\d+)` | **yes / yes** (MODE1 drops TCP) |
| TIMER | `TIMER,{n},{n}#` | cxzt/param | `TIMER#` | cxzt `F:(\d+)` / `TIMER:(\d+)` | no |
| HBT | `HBT,{n},{n}#` | cxzt/param | `HBT#` | cxzt `H:(\d+)` / `HBT:(\d+)` | no |
| SENDS | `SENDS,{n}#` | param | `SENDS#` | `SENDS:(\d+)` | no |
| SLPDISCONNECT | `SZCS#SLPDISCONNECT={n}` | query | `CXCS#SLPDISCONNECT` | `SLPDISCONNECT=(\d+)` | no |
| GPS_RST_TIME | `SZCS#GPS_RST_TIME={n}` | query | `CXCS#GPS_RST_TIME` | `GPS_RST_TIME=(\d+)` | no |
| VIBCHK | `SZCS#VIBCHK={a}:{b}` | query | `CXCS#VIBCHK` | `VIBCHK=(\d+:\d+)` | no |
| ACCLINE | `SZCS#ACCLINE={n}` | query | `CXCS#ACCLINE` | `ACCLINE=(\d+)` | no |
| SENALM | `SENALM,{v}#` | query | `SENALM#` | `SENALM:(\w+)` | no |
| MOVING | `MOVING,{v}#` | query | `MOVING#` | `MOVING:(\w+...)` | no |

Value-normalization note: the same setting reads back in different encodings
(TIMER set as `TIMER,1,1#`, read as `F:1\|1` via cxzt, `TIMER:1,3600` via PARAM,
`TIMER ACC ON:1s` via `TIMER#`). `norm()` collapses these to one comparable form
(e.g. the integer LOC seconds) so the diff is reliable.

## The one real decision: how deep to query on connect

Querying *every* setting exactly = `cxzt#` + `PARAM#` + 6 individual queries =
~8 round-trips per device on every connect (×11 trackers, on a flaky link this is
a lot, and competes with the LOC stream). Options:

- **A — bulk + believed (recommended).** Always `cxzt#` (+ `PARAM#` if a
  param-only key is in the desired set). For the 6 query-only settings, trust the
  persisted believed-state; only fall back to individual `CXCS#`/`<NAME>#`
  queries when believed-state is empty (first-ever connect) or on an explicit
  "re-audit" from the management page. Cheap in steady state; self-heals on first
  contact and on demand.
- **B — full query every connect.** Exact, but 8 round-trips/device/connect.
- **C — bulk only, never individual.** Cheapest; the 6 unreadable settings are
  pure fire-and-forget (set once when believed-state is empty, then trusted).

Recommendation: **A**. It reconciles the readable settings precisely every time,
keeps the unreadable ones correct via believed-state + SETOK confirmation, and
only pays the 6-query cost rarely.

## Migration (incremental, each step testable on the gt06 simulator)

1. **Introduce the tables + `reconcile()` with believed-state seeded from the
   current builders** — i.e. make the table reproduce today's behaviour exactly
   (same commands, same order). Land behind a flag; verify `test_gt06_sim.py`
   unchanged.
2. **Route login/state-change through `reconcile()`** instead of the imperative
   `_active_cmds`/`_idle_cmds`/`_overnight_cmds` + cxzt pushes. Keep the storm
   guard (`OVERNIGHT_FREQ_MAX_RETRIES`) and MODE one-shot semantics.
3. **Add the query layer** (cxzt# + PARAM#) and observed-state diffing, so only
   changed settings are sent (this is where the reconnect-churn bug dies).
4. **Close the coverage gaps**: fill the TBD desired values (ACCLINE in day
   states, SENDS/GPS_RST in V667 sleep) once confirmed, and add SENALM/MOVING to
   all states. Now the desired table is the single source of truth.

## Invariants to preserve (don't regress)

- MODE1 must stay **one-shot** — sending it tears down TCP and a per-login resend
  is a reconnect storm (`_idle_cmds` comment).
- Overnight Freq re-push must stay **storm-guarded** (`OVERNIGHT_FREQ_MAX_RETRIES`)
  for V6.6x firmware that refuses the freq arg.
- `SLPDISCONNECT=0` stays idempotent / safe every connect.
- Idle privacy: idle/sleep states still must never record lat/lon (orthogonal to
  this, but don't let a refactor touch it).
- Don't re-toggle GPS-affecting settings on a reconnect of an already-correct
  device (the whole point — diff-based send guarantees this).

## Open questions for review

1. Query depth on connect — **A / B / C** above (recommend A).
2. The **TBD** desired values: what should ACCLINE be in tracking/idle, and what
   should SENDS / GPS_RST_TIME be in V667 sleep? (Needs vendor semantics — fold
   into the GPS-accuracy vendor question list?)
3. Believed-state scope — per-IMEI persisted (survives restart) vs per-connection
   (re-audited each connect). Recommend persisted, refreshed by bulk query.
