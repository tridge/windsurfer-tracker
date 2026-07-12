#!/usr/bin/env python3
"""Fleet-wide GT06 anomaly analysis from raw v2 packet logs.

Single streaming pass over one or more daily gt06.log rotations, all units at
once. Reconstructs each unit's commanded mode (idle/active/overnight) from the
outgoing 0x80 commands in the log, then checks the observed behaviour against
what that mode promises. Anomaly categories:

  excess-drain    SoC burn over a mode segment well above the calibrated model
                  (or the unit's own pre-event baseline)
  voltage-drop    sudden OCV step between same-load readings
  idle-loc        LOC bursts while commanded idle (beyond the expected one
                  cached-fix upload per TIMER T2)
  replay          blind-buffer playback while tracking: did it drain (catch
                  up) or stall / persist to disconnect?
  persistent-lag  sustained delayed GPS time vs server clock outside replay
                  episodes
  reboot          device serial-counter regression at login (serials survive
                  TCP reconnects, so a regression = firmware restart)
  dropout         contact silence > --gap, classified reconnect/stall,
                  fleet-wide login clusters tagged as server restarts
  settings-drift  cxzt-reported TIMER/HBT disagrees with the commanded mode
  sos             alarm frames

Usage:
  # pre-event per-unit baseline (idle drain / idle-LOC background):
  scripts/gt06_regatta_report.py old_logs/gt06.log.2026-06-2[789] \
      old_logs/gt06.log.2026-07-0[1-4] --baseline-out baseline.json
  # event analysis:
  scripts/gt06_regatta_report.py old_logs/gt06.log.2026-07-0[5-9] \
      old_logs/gt06.log.2026-07-1[01] --baseline baseline.json \
      --out gt06/regatta2026_analysis [--plots]
"""
import sys
import re
import json
import csv
import bisect
import argparse
import statistics
from pathlib import Path
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gt06_dump import (read_packets, detect_format, validate_frame,  # noqa: E402
                       fmt_time, ConnIdentityTracker)
from gt06_soc import SocModel, gid_of  # noqa: E402
from protocol_GT06 import (gt06_parse_location, gt06_parse_heartbeat,  # noqa: E402
                           GT06_PROTO_LOG_IMEI)

# cxzt/CXCS reply fields
_FW_RE = re.compile(rb"([A-Za-z0-9][A-Za-z0-9_.]*?V6\.?\d+)-")
_BT = re.compile(rb"\*BT:(\d+)")
_F_RE = re.compile(rb"\*F:(\d+)\|(\d+)")
_H_RE = re.compile(rb"\*H:(\d+)")
_STAT_V = re.compile(rb"Battery:(\d+\.\d+)V")
_ACC = re.compile(rb"ACC:(ON|OFF)")
_GPS = re.compile(rb"GPS:(ON|OFF)")
_TIMER_RE = re.compile(r"TIMER,(\d+),(\d+)")

MODE_IDLE, MODE_ACTIVE, MODE_OVERNIGHT = "idle", "active", "overnight"


def _hm(ts):
    return fmt_time(ts)[5:19]


def _dur(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def parse_when(v):
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    import time as _t
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _t.mktime(_t.strptime(v, fmt))
        except ValueError:
            continue
    raise SystemExit(f"can't parse time {v!r} (epoch or 'YYYY-MM-DD HH:MM')")


def first_ts(path):
    with open(path, "rb") as f:
        fmt = detect_format(f)
        for ts, _c, _o, _f in read_packets(f, fmt=fmt):
            return ts
    return float("inf")


class Unit:
    """All per-unit streaming state and result accumulators."""

    def __init__(self, gid, imei):
        self.gid = gid
        self.imei = imei
        self.fw = None
        # mode reconstruction
        self.mode = None              # current commanded mode
        self.mode_since = None
        self.mode_source = None       # 'cmd' or 'inferred'
        self.transitions = []         # [(ts, mode, source)]
        self.bounces = []             # MODE1 pushes (ts)
        self.rate_events = []         # (ts, t1) TIMER pushes
        self.reset_cmds = []          # RESET# pushes (ts)
        # contact / session
        self.first_ts = None
        self.last_in = None           # last incoming frame ts
        self.last_conn = None
        self.last_login = None
        self.last_serial = None
        self.run_start = None         # current tight-contact run start
        self.conn_secs = 0.0
        self.logins = []              # [(ts, gap, reboot, server_reset)]
        self.dropouts = []            # dicts
        # voltage / battery
        self.readings = []            # (ts, volts, src, gps, acc)
        self.charge_iv = []           # merged [start, end] charging intervals
        # LOC / lag
        self.loc_total = 0
        self.loc_active = 0
        self.loc_idle = 0
        self.loc_replay = 0
        self.idle_loc_normal = 0
        self.lag_hist = {}            # int(lag)s -> count (active LOC only)
        self.minute_cur = None        # (minute_epoch, [lags])
        self.minute_lag = []          # (minute_epoch, median, max, n)
        self.lag_label = None         # V/L interleave state
        self.prev_gps = None
        self.replay_ep = None
        self.replay_eps = []
        self.idle_ep = None
        self.idle_eps = []
        self.last_idle_loc = None
        self.recent_locs = deque(maxlen=8)
        self.sos = []                 # (ts, label)
        self.drift = {}               # observed (f1,f2,hbt) mismatch -> first ts
        self.anomalies = []

    # ---- mode ---------------------------------------------------------------
    def set_mode(self, ts, mode, source="cmd"):
        if mode == self.mode:
            return
        self.close_idle_ep(ts)
        self.close_replay_ep(ts, "mode-change")
        self.mode = mode
        self.mode_since = ts
        self.mode_source = source
        self.transitions.append((ts, mode, source))
        self.last_idle_loc = None

    def feed_cmd(self, ts, txt):
        if txt.startswith("SENDS,1"):
            self.set_mode(ts, MODE_IDLE)
        elif txt.startswith("SENDS,0"):
            self.set_mode(ts, MODE_ACTIVE)
        elif txt.startswith("MODE4,") or txt.startswith("MODE5,"):
            self.set_mode(ts, MODE_OVERNIGHT)
        elif txt.startswith("MODE1"):
            self.bounces.append(ts)
            if self.idle_ep and (ts - self.idle_ep["last"]) < 300:
                self.idle_ep["remediated"] = True
        elif txt.startswith("RESET"):
            self.reset_cmds.append(ts)
        else:
            m = _TIMER_RE.match(txt)
            if m:
                t1 = int(m.group(1))
                self.rate_events.append((ts, t1))
                if self.replay_ep is not None and t1 <= 3:
                    self.replay_ep["drain_cmds"] += 1

    # ---- charging -----------------------------------------------------------
    def feed_charging(self, ts):
        if self.charge_iv and ts - self.charge_iv[-1][1] < 120:
            self.charge_iv[-1][1] = ts
        else:
            self.charge_iv.append([ts, ts])

    def is_charging_near(self, ts):
        """True if a charging heartbeat fell in [ts-60, ts+300] (surface
        charge / on-charger exclusion window for voltage readings)."""
        i = bisect.bisect_right([iv[0] for iv in self.charge_iv], ts + 60)
        for iv in self.charge_iv[max(0, i - 2):i + 1]:
            if iv[0] - 60 <= ts <= iv[1] + 300:
                return True
        return False

    # ---- LOC ----------------------------------------------------------------
    def feed_loc(self, ts, lag, spd, sats, valid, cfg):
        self.loc_total += 1
        self.recent_locs.append(ts)
        if self.mode == MODE_ACTIVE:
            self.loc_active += 1
            self._lag_account(ts, lag)
            self._replay_track(ts, lag, cfg)
        elif self.mode in (MODE_IDLE, MODE_OVERNIGHT):
            self.loc_idle += 1
            self._idle_loc(ts, lag, spd, sats, cfg)
        if self.prev_gps is not None and (ts - lag) < self.prev_gps - 2.0:
            if self.replay_ep is not None:
                self.replay_ep["back"] += 1
        self.prev_gps = ts - lag

    def _lag_account(self, ts, lag):
        self.lag_hist[int(min(max(lag, 0), 7200))] = \
            self.lag_hist.get(int(min(max(lag, 0), 7200)), 0) + 1
        minute = int(ts // 60) * 60
        if self.minute_cur is None or self.minute_cur[0] != minute:
            self._flush_minute()
            self.minute_cur = (minute, [])
        self.minute_cur[1].append(lag)

    def _flush_minute(self):
        if self.minute_cur and self.minute_cur[1]:
            lags = self.minute_cur[1]
            self.minute_lag.append((self.minute_cur[0],
                                    statistics.median(lags), max(lags), len(lags)))
        self.minute_cur = None

    def _replay_track(self, ts, lag, cfg):
        label = "L" if lag > cfg.lag else ("V" if lag <= cfg.live else None)
        if lag > cfg.lag:
            self.loc_replay += 1
        ep = self.replay_ep
        if ep is None:
            if label == "L":
                self.replay_ep = {"t0": ts, "start_lag": lag, "max_lag": lag,
                                  "n": 1, "n_lag": 1, "v2l": 0, "back": 0,
                                  "drain_cmds": 0, "last": ts, "last_lag": lag,
                                  "last_high": ts}
        else:
            ep["n"] += 1
            ep["max_lag"] = max(ep["max_lag"], lag)
            ep["last"] = ts
            ep["last_lag"] = lag
            if lag > cfg.lag:
                ep["n_lag"] += 1
                ep["last_high"] = ts
            if self.lag_label == "V" and label == "L":
                ep["v2l"] += 1
            # drained only once the lag stays low for a hold period — the V667
            # firmware interleaves live fixes into the backlog, so a single
            # low-lag fix does not mean the buffer is empty
            if lag <= cfg.restore and ts - ep["last_high"] >= cfg.drained_hold:
                self.close_replay_ep(ts, "drained")
        if label is not None:
            self.lag_label = label

    def close_replay_ep(self, ts, how):
        ep = self.replay_ep
        if ep is None:
            return
        self.replay_ep = None
        ep["t1"] = ts
        ep["result"] = how
        if how == "drained" and (ep["v2l"] > 0 or ep["back"] > 0):
            ep["result"] = "drained-interleaved"
        self.replay_eps.append(ep)

    def _idle_loc(self, ts, lag, spd, sats, cfg):
        if self.mode_since and ts - self.mode_since < cfg.idle_grace:
            return
        if self.bounces and ts - self.bounces[-1] < cfg.idle_grace:
            return
        ep = self.idle_ep
        if ep is None:
            # a single cached-fix upload per TIMER T2 is the expected idle
            # behaviour, not an anomaly
            if self.last_idle_loc is None or ts - self.last_idle_loc >= cfg.t2_min:
                self.idle_loc_normal += 1
                self.last_idle_loc = ts
                return
            self.idle_ep = {"t0": self.last_idle_loc, "last": ts, "n": 2,
                            "stuck": False, "remediated": False,
                            "max_spd": spd, "max_lag": lag}
        else:
            if ts - ep["last"] > 300:
                self.close_idle_ep(ep["last"])
                self.last_idle_loc = ts
                return
            ep["n"] += 1
            ep["last"] = ts
            ep["max_spd"] = max(ep["max_spd"], spd)
            ep["max_lag"] = max(ep["max_lag"], lag)
            if len(self.recent_locs) >= 5 and ts - self.recent_locs[-5] <= 60 \
                    and ts - ep["t0"] >= 60:
                ep["stuck"] = True
        self.last_idle_loc = ts

    def close_idle_ep(self, ts):
        ep = self.idle_ep
        if ep is None:
            return
        self.idle_ep = None
        ep["t1"] = ts
        self.idle_eps.append(ep)

    # ---- contact / login ----------------------------------------------------
    def feed_contact(self, ts, conn_id, cfg, is_login=False, serial=None):
        prev = self.last_in
        if self.first_ts is None:
            self.first_ts = ts
        if prev is not None:
            gap = ts - prev
            if gap <= cfg.gap:
                self.conn_secs += gap
            else:
                run = prev - (self.run_start or prev)
                relogin = is_login or (self.last_login or -1) > prev \
                    or conn_id != self.last_conn
                self.dropouts.append({
                    "start": prev, "end": ts, "dur": gap,
                    "state": "active" if run >= cfg.active_run else "idle",
                    "kind": "reconnect" if relogin else "stall"})
                self.close_replay_ep(prev, "disconnected-lagged")
                self.close_idle_ep(prev)
            if gap > cfg.active_secs:
                self.run_start = ts
        else:
            self.run_start = ts
        if is_login:
            reboot = (serial is not None and self.last_serial is not None
                      and serial <= 5 and 20 <= self.last_serial <= 65500)
            server_reset = any(0 <= ts - r <= 600 for r in self.reset_cmds[-3:])
            self.logins.append({"ts": ts, "gap": (ts - prev) if prev else 0.0,
                                "reboot": reboot, "server_reset": server_reset})
            self.last_login = ts
            self.lag_label = None
            self.prev_gps = None
        if serial is not None:
            self.last_serial = serial
        self.last_in = ts
        self.last_conn = conn_id

    # ---- finalize helpers ---------------------------------------------------
    def segments(self, end_ts):
        segs = []
        for i, (ts, mode, _src) in enumerate(self.transitions):
            t1 = self.transitions[i + 1][0] if i + 1 < len(self.transitions) else end_ts
            segs.append((ts, t1, mode))
        return segs

    def mode_hours(self, end_ts):
        h = {MODE_IDLE: 0.0, MODE_ACTIVE: 0.0, MODE_OVERNIGHT: 0.0}
        for t0, t1, mode in self.segments(end_ts):
            h[mode] += (t1 - t0) / 3600.0
        return h

    def lag_pct(self, q):
        total = sum(self.lag_hist.values())
        if not total:
            return None
        need = total * q
        run = 0
        for lag in sorted(self.lag_hist):
            run += self.lag_hist[lag]
            if run >= need:
                return lag
        return max(self.lag_hist)


class FleetScan:
    def __init__(self, cfg, soc):
        self.cfg = cfg
        self.soc = soc
        self.units = {}               # gid -> Unit
        self.ident = ConnIdentityTracker()
        self.pending = {}             # conn_id -> [event tuples] pre-identity
        self.first = None
        self.last = 0.0

    def unit_for(self, imei):
        gid = gid_of(imei)
        u = self.units.get(gid)
        if u is None:
            u = self.units[gid] = Unit(gid, imei)
        return u

    def run(self, paths):
        paths = sorted(paths, key=first_ts)
        prev_file_last = None
        for path in paths:
            file_last = None
            with open(path, "rb") as f:
                fmt = detect_format(f)
                if fmt != "v2":
                    raise SystemExit(f"{path}: not a v2 log")
                for ts, conn_id, outgoing, frame in read_packets(f, fmt=fmt):
                    res = validate_frame(frame)
                    if not res:
                        continue
                    if file_last is None and prev_file_last is not None \
                            and ts - prev_file_last > self.cfg.session_gap:
                        self.flush_open(prev_file_last)
                        self.pending.clear()
                    file_last = ts
                    self.feed(ts, conn_id, outgoing, res)
            if file_last is not None:
                prev_file_last = file_last
        self.finalize()

    def feed(self, ts, conn_id, outgoing, res):
        proto, data = res[0], res[1]
        self.ident.feed(conn_id, outgoing, res)
        if (self.cfg.win0 is not None and ts < self.cfg.win0) or \
                (self.cfg.win1 is not None and ts > self.cfg.win1):
            return
        imei = self.ident.get(conn_id)
        if imei is None:
            buf = self.pending.setdefault(conn_id, [])
            if len(buf) < 50000:
                buf.append((ts, conn_id, outgoing, res))
            return
        buf = self.pending.pop(conn_id, None)
        if buf:
            for item in buf:
                self.dispatch(imei, *item)
        self.dispatch(imei, ts, conn_id, outgoing, res)

    def dispatch(self, imei, ts, conn_id, outgoing, res):
        proto, data, serial, _ok = res
        cfg = self.cfg
        if proto == GT06_PROTO_LOG_IMEI:
            return                      # synthetic marker: identity only
        u = self.unit_for(imei)
        if outgoing:
            if proto == 0x80 and len(data) >= 5:
                txt = data[5:].split(b"\x00")[0].decode("ascii", "replace")
                u.feed_cmd(ts, txt)
            return
        if self.first is None:
            self.first = ts
        self.last = max(self.last, ts)
        # replay-episode LOC silence timeout
        if u.replay_ep is not None and ts - u.replay_ep["last"] > cfg.replay_silence \
                and proto not in (0x12, 0x22):
            u.close_replay_ep(u.replay_ep["last"], "stalled")
        u.feed_contact(ts, conn_id, cfg, is_login=(proto == 0x01), serial=serial)

        if proto in (0x12, 0x22):
            loc = gt06_parse_location(data)
            if loc and loc.get("ts"):
                u.feed_loc(ts, ts - loc["ts"], loc["speed_kmh"] / 1.852,
                           loc["satellites"], loc["gps_valid"], cfg)
        elif proto == 0x13:
            hb = gt06_parse_heartbeat(data)
            if hb.get("charging"):
                u.feed_charging(ts)
        elif proto in (0x16, 0x23):
            u.sos.append(ts)
        elif proto == 0x15:
            self.feed_cmdresp(u, ts, data)

    def feed_cmdresp(self, u, ts, data):
        m = _FW_RE.search(data)
        if m:
            u.fw = m.group(1).decode()
        gps = acc = None
        ma = _ACC.search(data)
        if ma:
            acc = ma.group(1) == b"ON"
        mg = _GPS.search(data)
        if mg:
            gps = mg.group(1) == b"ON"
        mv = _BT.search(data)
        if mv:
            u.readings.append((ts, int(mv.group(1)) / 1000.0, "bt", gps, acc))
        else:
            msv = _STAT_V.search(data)
            if msv:
                u.readings.append((ts, float(msv.group(1)), "stat", gps, acc))
        # settings cross-check from the cxzt banner
        mf = _F_RE.search(data)
        if mf and u.mode and u.mode_since and ts - u.mode_since > 300:
            f1, f2 = int(mf.group(1)), int(mf.group(2))
            mh = _H_RE.search(data)
            hbt = int(mh.group(1)) if mh else None
            ok = (u.mode == MODE_IDLE and (f1, f2) == (60, 1800)) or \
                 (u.mode == MODE_ACTIVE and f1 == f2 and f1 <= 3) or \
                 (u.mode == MODE_OVERNIGHT)
            if not ok:
                key = (u.mode, f1, f2, hbt)
                u.drift.setdefault(key, ts)
        elif mf and u.mode is None:
            # bootstrap: infer the pre-window mode from the device's settings
            f1, f2 = int(mf.group(1)), int(mf.group(2))
            if (f1, f2) == (60, 1800):
                u.set_mode(ts, MODE_IDLE, source="inferred")
            elif f1 == f2 and f1 <= 3:
                u.set_mode(ts, MODE_ACTIVE, source="inferred")

    def flush_open(self, cutoff):
        for u in self.units.values():
            if u.last_in is not None and cutoff - u.last_in > self.cfg.gap:
                run = u.last_in - (u.run_start or u.last_in)
                u.dropouts.append({
                    "start": u.last_in, "end": None, "dur": cutoff - u.last_in,
                    "state": "active" if run >= self.cfg.active_run else "idle",
                    "kind": "no-return"})
            u.close_replay_ep(u.last_in or cutoff, "log-end")
            u.close_idle_ep(u.last_in or cutoff)
            u._flush_minute()

    def finalize(self):
        self.flush_open(self.last)
        # server-restart clustering on login times across the fleet
        all_logins = sorted((lg["ts"], u.gid) for u in self.units.values()
                            for lg in u.logins)
        times = [t for t, _g in all_logins]
        self.restarts = []
        for u in self.units.values():
            for lg in u.logins:
                i0 = bisect.bisect_left(times, lg["ts"] - self.cfg.restart_window)
                i1 = bisect.bisect_right(times, lg["ts"] + self.cfg.restart_window)
                units = {g for _t, g in all_logins[i0:i1]}
                lg["cluster"] = len(units)
                lg["restart"] = len(units) >= self.cfg.restart_min
        # merge clustered logins into fleet events and classify each: a wave of
        # serial-regression reboots is a NETWORK/mass-device-reboot event (the
        # devices restarted), a wave of plain re-logins is a server restart
        # (sockets reset, device serials continue)
        ev = []
        clustered = sorted((lg["ts"], lg) for u in self.units.values()
                           for lg in u.logins if lg["restart"])
        for t, lg in clustered:
            if ev and t - ev[-1]["t1"] <= 300:
                ev[-1]["t1"] = t
                ev[-1]["logins"].append(lg)
            else:
                ev.append({"t0": t, "t1": t, "logins": [lg]})
        self.restarts = []
        for e in ev:
            reboots = sum(1 for lg in e["logins"] if lg["reboot"])
            kind = "mass-device-reboot" if reboots >= max(3, len(e["logins"]) // 2) \
                else "server-restart"
            for lg in e["logins"]:
                lg["event"] = kind
            self.restarts.append({"ts": e["t0"], "end": e["t1"], "kind": kind,
                                  "logins": len(e["logins"]), "reboots": reboots})


# ---------------- post-pass detectors (voltage-based) ------------------------

def mode_at(segs, ts):
    for t0, t1, mode in segs:
        if t0 <= ts < t1:
            return mode
    return None


def drain_windows(u, segs, soc, cfg, baseline=None):
    """Per-mode-segment SoC drain fits vs expectation. Returns (anoms, stats)."""
    anoms = []
    stats = {MODE_IDLE: [], MODE_ACTIVE: []}
    for t0, t1, mode in segs:
        if mode not in (MODE_IDLE, MODE_ACTIVE) or \
                t1 - t0 < (cfg.drain_min_sec if mode == MODE_ACTIVE
                           else cfg.idle_drain_min_sec):
            continue
        pts = []
        for ts, v, _src, gps, acc in u.readings:
            if not (t0 <= ts < t1) or u.is_charging_near(ts):
                continue
            if mode == MODE_IDLE and not (gps is False and acc is False):
                continue
            ocv = soc.ocv(u.gid, v, soc.load_ma("track" if mode == MODE_ACTIVE
                                                else "idle"))
            if ocv > 4.20:
                continue
            pts.append((ts, soc.soc(ocv)))
        # active drains are steep enough for short windows; idle drain is
        # ~0.1-0.2%/h (a few mV/h against 10mV STATUS quantization), so it
        # needs much longer windows to resolve
        wsec = cfg.drain_window_sec if mode == MODE_ACTIVE \
            else cfg.idle_drain_window_sec
        wmin = cfg.drain_min_sec if mode == MODE_ACTIVE else cfg.idle_drain_min_sec
        w0 = t0
        while w0 < t1:
            w1 = min(w0 + wsec, t1)
            wpts = [(ts, s) for ts, s in pts if w0 <= ts < w1]
            if len(wpts) >= 3 and wpts[-1][0] - wpts[0][0] >= wmin:
                hrs = [(ts - wpts[0][0]) / 3600.0 for ts, _s in wpts]
                socs = [s for _ts, s in wpts]
                n = len(hrs)
                mh, ms = sum(hrs) / n, sum(socs) / n
                den = sum((h - mh) ** 2 for h in hrs)
                if den > 0:
                    slope = sum((h - mh) * (s - ms) for h, s in zip(hrs, socs)) / den
                    drain = -slope   # %/h burn
                    stats[mode].append(drain)
                    exp = soc.expected_drain_pct_h(
                        u.gid, "track" if mode == MODE_ACTIVE else "idle")
                    ref = exp
                    if mode == MODE_IDLE and baseline:
                        b = baseline.get(u.gid, {}).get("idle_drain_pct_h")
                        if b is not None:
                            ref = max(exp, b)
                    thr = max(cfg.drain_factor * ref, exp + cfg.drain_abs)
                    if drain > thr:
                        anoms.append({
                            "category": "excess-drain", "t0": wpts[0][0],
                            "t1": wpts[-1][0], "severity": round(drain - ref, 2),
                            "summary": f"{mode} drain {drain:.2f}%/h vs expected "
                                       f"{ref:.2f}%/h ({len(wpts)} readings)",
                            "details": {"mode": mode, "drain_pct_h": round(drain, 3),
                                        "expected_pct_h": round(ref, 3),
                                        "n": len(wpts)}})
            w0 = w1
    return anoms, stats


def voltage_drops(u, segs, soc, cfg):
    anoms = []
    prev = {}   # state key -> (ts, ocv)
    rd = [(ts, v, gps, acc) for ts, v, _s, gps, acc in u.readings
          if not u.is_charging_near(ts)]
    for i, (ts, v, gps, acc) in enumerate(rd):
        mode = mode_at(segs, ts)
        if mode is None:
            continue
        load = soc.load_ma("track" if mode == MODE_ACTIVE else "idle")
        ocv = soc.ocv(u.gid, v, load)
        if ocv > 4.20:
            continue
        key = (mode, gps, acc)
        p = prev.get(key)
        prev[key] = (ts, ocv)
        if p is None or ts - p[0] > cfg.drop_pair_sec:
            continue
        dv = ocv - p[1]
        if dv < -cfg.drop_mv / 1000.0:
            # transient sag if the next same-key reading recovers
            rec = None
            for ts2, v2, gps2, acc2 in rd[i + 1:i + 40]:
                if (mode_at(segs, ts2), gps2, acc2) == key:
                    rec = soc.ocv(u.gid, v2, load) - ocv
                    break
            kind = "transient-sag" if (rec is not None and rec > 0.040) else "drop"
            reboot_near = any(abs(lg["ts"] - ts) < 600 and lg["reboot"]
                              for lg in u.logins)
            anoms.append({
                "category": "voltage-drop", "t0": p[0], "t1": ts,
                "severity": round(-dv * 1000, 1),
                "summary": f"OCV {kind} {dv*1000:.0f}mV in {_dur(ts-p[0])}"
                           f" ({mode}{', near reboot' if reboot_near else ''})",
                "details": {"kind": kind, "dv_mv": round(dv * 1000, 1),
                            "mode": mode, "near_reboot": reboot_near}})
    return anoms


def persistent_lag(u, cfg):
    anoms = []
    ep_iv = [(e["t0"], e.get("t1", e["last"])) for e in u.replay_eps]

    def in_ep(ts):
        return any(a - 60 <= ts <= b + 60 for a, b in ep_iv)
    run = []
    for minute, med, mx, n in u.minute_lag:
        if med > cfg.plag_sec and not in_ep(minute):
            if run and minute - run[-1][0] > 180:
                run = []
            run.append((minute, med))
            if len(run) >= cfg.plag_min_minutes and \
                    run[-1][0] - run[0][0] >= (cfg.plag_min_minutes - 1) * 60:
                anoms.append({
                    "category": "persistent-lag", "t0": run[0][0],
                    "t1": minute + 60,
                    "severity": round(statistics.median(m for _t, m in run), 1),
                    "summary": f"lag median {statistics.median(m for _t, m in run):.0f}s "
                               f"sustained {_dur(minute + 60 - run[0][0])} outside replay",
                    "details": {"minutes": len(run)}})
                run = []
    return anoms


# ---------------- reporting ---------------------------------------------------

def load_users(path):
    try:
        us = json.load(open(path))
    except Exception:
        return {}
    out = {}
    for k, v in us.get("users", {}).items():
        lid = v.get("_last_id", "")
        if isinstance(lid, str) and re.fullmatch(r"G\d{6}", lid):
            out[lid] = v.get("name", "")
    return out


def fw_class(fw):
    if not fw:
        return "?"
    m = re.search(r"V6\.?(\d+)$", fw)
    return f"V6{'.' if '.' in fw[-5:] else ''}{m.group(1)}" if m else fw


def build_results(scan, soc, cfg, baseline, names):
    end = scan.last
    units_out = {}
    anomalies = []
    for gid in sorted(scan.units):
        u = scan.units[gid]
        segs = u.segments(end)
        mh = u.mode_hours(end)
        drains, dstats = drain_windows(u, segs, soc, cfg, baseline)
        anoms = list(drains)
        anoms += voltage_drops(u, segs, soc, cfg)
        anoms += persistent_lag(u, cfg)
        for e in u.idle_eps:
            if e["n"] < cfg.idle_loc_min:
                continue
            rate = e["n"] / max((e["t1"] - e["t0"]) / 60.0, 1e-9)
            anoms.append({
                "category": "idle-loc", "t0": e["t0"], "t1": e["t1"],
                "severity": e["n"],
                "summary": f"{e['n']} LOC in {_dur(e['t1']-e['t0'])} while idle "
                           f"({rate:.1f}/min{', STUCK' if e['stuck'] else ''}"
                           f"{', bounced' if e['remediated'] else ''})",
                "details": {"n": e["n"], "per_min": round(rate, 2),
                            "stuck": e["stuck"], "remediated": e["remediated"],
                            "max_spd_kn": round(e["max_spd"], 1),
                            "max_lag_s": round(e["max_lag"], 1)}})
        for e in u.replay_eps:
            dur = e.get("t1", e["last"]) - e["t0"]
            anoms.append({
                "category": "replay", "t0": e["t0"], "t1": e.get("t1", e["last"]),
                "severity": round(e["max_lag"], 0),
                "summary": f"replay maxlag {e['max_lag']:.0f}s -> {e['result']} "
                           f"in {_dur(dur)} ({e['n_lag']}/{e['n']} lagged fixes, "
                           f"{e['drain_cmds']} drain cmds, intl {e['v2l']}, "
                           f"back {e['back']}, end lag {e['last_lag']:.0f}s)",
                "details": {k: (round(v, 1) if isinstance(v, float) else v)
                            for k, v in e.items()}})
        for lg in u.logins:
            if lg["reboot"]:
                anoms.append({
                    "category": "reboot", "t0": lg["ts"], "t1": lg["ts"],
                    "severity": 1,
                    "summary": "device reboot (serial regression)"
                               + (" after server RESET#" if lg["server_reset"] else
                                  (" in mass-device-reboot event"
                                   if lg.get("event") == "mass-device-reboot"
                                   else " UNEXPECTED")),
                    "details": {"server_reset": lg["server_reset"],
                                "event": lg.get("event"),
                                "gap_s": round(lg["gap"], 0)}})
        for d in u.dropouts:
            in_restart = d["end"] is not None and any(
                r["ts"] - cfg.restart_window <= d["end"] <= r["end"] + cfg.restart_window
                for r in scan.restarts if r["kind"] == "server-restart")
            if in_restart:
                continue
            anoms.append({
                "category": "dropout", "t0": d["start"],
                "t1": d["end"] or d["start"] + d["dur"],
                "severity": round(d["dur"] / 60.0, 1),
                "summary": f"{d['state']} silence {_dur(d['dur'])} -> {d['kind']}",
                "details": d})
        for (mode, f1, f2, hbt), ts0 in sorted(u.drift.items(), key=lambda kv: kv[1]):
            anoms.append({
                "category": "settings-drift", "t0": ts0, "t1": ts0, "severity": 0,
                "summary": f"cxzt TIMER {f1}|{f2} HBT {hbt} while commanded {mode}",
                "details": {"mode": mode, "f1": f1, "f2": f2, "hbt": hbt}})
        for ts in u.sos:
            anoms.append({"category": "sos", "t0": ts, "t1": ts, "severity": 0,
                          "summary": "alarm/SOS frame", "details": {}})
        for a in anoms:
            a["gid"] = gid
        anomalies += anoms
        u.anomalies = anoms

        rest = [(ts, v) for ts, v, _s, gps, acc in u.readings
                if gps is False and acc is False and not u.is_charging_near(ts)]
        units_out[gid] = {
            "imei": u.imei, "name": names.get(gid, ""), "fw": u.fw or "?",
            "fw_class": fw_class(u.fw), "cap_class": soc.cap_class(gid),
            "capacity_mah": soc.capacity_mah(gid),
            "hours_conn": round(u.conn_secs / 3600.0, 2),
            "hours_active": round(mh[MODE_ACTIVE], 2),
            "hours_idle": round(mh[MODE_IDLE], 2),
            "hours_overnight": round(mh[MODE_OVERNIGHT], 2),
            "loc_total": u.loc_total, "loc_active": u.loc_active,
            "loc_idle": u.loc_idle, "idle_loc_normal": u.idle_loc_normal,
            "loc_replay": u.loc_replay,
            "replay_eps": len(u.replay_eps),
            "replay_drained": sum(1 for e in u.replay_eps
                                  if e["result"].startswith("drained")),
            "replay_unresolved": sum(1 for e in u.replay_eps
                                     if not e["result"].startswith("drained")),
            "lag_p50": u.lag_pct(0.50), "lag_p95": u.lag_pct(0.95),
            "lag_max": max(u.lag_hist) if u.lag_hist else None,
            "logins": len(u.logins),
            "restart_logins": sum(1 for lg in u.logins if lg["restart"]),
            "solo_logins": sum(1 for lg in u.logins if not lg["restart"]),
            "reboots": sum(1 for lg in u.logins if lg["reboot"]),
            "dropouts": len(u.dropouts),
            "drain_idle_med": round(statistics.median(dstats[MODE_IDLE]), 3)
            if dstats[MODE_IDLE] else None,
            "drain_track_med": round(statistics.median(dstats[MODE_ACTIVE]), 3)
            if dstats[MODE_ACTIVE] else None,
            "drain_idle_exp": round(soc.expected_drain_pct_h(gid, "idle"), 3),
            "drain_track_exp": round(soc.expected_drain_pct_h(gid, "track"), 3),
            "v_first": rest[0][1] if rest else None,
            "v_last": rest[-1][1] if rest else None,
            "soc_first": round(soc.soc_of(gid, rest[0][1]), 1) if rest else None,
            "soc_last": round(soc.soc_of(gid, rest[-1][1]), 1) if rest else None,
            "anomaly_counts": _count_by(anoms, "category"),
        }
    anomalies.sort(key=lambda a: a["t0"])
    return units_out, anomalies


def _count_by(items, key):
    out = {}
    for it in items:
        out[it[key]] = out.get(it[key], 0) + 1
    return out


def cross_tabs(units_out):
    tabs = {}
    for gid, u in units_out.items():
        key = f"{u['fw_class']}/{u['cap_class']}"
        t = tabs.setdefault(key, {"units": 0, "hours_active": 0.0,
                                  "hours_idle": 0.0, "counts": {},
                                  "idle_drains": [], "track_drains": [],
                                  "lag_p95s": [], "gids": []})
        t["units"] += 1
        t["gids"].append(gid)
        t["hours_active"] += u["hours_active"]
        t["hours_idle"] += u["hours_idle"] + u["hours_overnight"]
        for c, n in u["anomaly_counts"].items():
            t["counts"][c] = t["counts"].get(c, 0) + n
        if u["drain_idle_med"] is not None:
            t["idle_drains"].append(u["drain_idle_med"])
        if u["drain_track_med"] is not None:
            t["track_drains"].append(u["drain_track_med"])
        if u["lag_p95"] is not None:
            t["lag_p95s"].append(u["lag_p95"])
    for t in tabs.values():
        t["idle_drain_med"] = round(statistics.median(t["idle_drains"]), 3) \
            if t["idle_drains"] else None
        t["track_drain_med"] = round(statistics.median(t["track_drains"]), 3) \
            if t["track_drains"] else None
        t["lag_p95_med"] = round(statistics.median(t["lag_p95s"]), 1) \
            if t["lag_p95s"] else None
        del t["idle_drains"], t["track_drains"], t["lag_p95s"]
        # normalized rates per 10 unit-hours in the relevant mode
        t["rates_per_10h"] = {}
        for c, n in t["counts"].items():
            denom = t["hours_active"] if c in ("replay", "persistent-lag") \
                else (t["hours_idle"] if c in ("idle-loc",) else
                      t["hours_active"] + t["hours_idle"])
            if denom > 0:
                t["rates_per_10h"][c] = round(10.0 * n / denom, 3)
    return tabs


def write_outputs(outdir, scan, units_out, anomalies, tabs, cfg, args):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "units.csv", "w", newline="") as f:
        cols = ["gid"] + [k for k in next(iter(units_out.values()))
                          if k != "anomaly_counts"] + ["anomaly_counts"]
        w = csv.writer(f)
        w.writerow(cols)
        for gid, u in units_out.items():
            w.writerow([gid] + [u[c] if c != "anomaly_counts"
                                else json.dumps(u["anomaly_counts"])
                                for c in cols[1:]])
    with open(outdir / "anomalies.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["gid", "category", "start", "end", "duration",
                    "severity", "summary", "details"])
        for a in anomalies:
            w.writerow([a["gid"], a["category"], fmt_time(a["t0"]),
                        fmt_time(a["t1"]), _dur(a["t1"] - a["t0"]),
                        a["severity"], a["summary"], json.dumps(a["details"])])
    summary = {
        "generated_from": [str(p) for p in args.logfiles],
        "window": [fmt_time(scan.first), fmt_time(scan.last)],
        "thresholds": {k: getattr(cfg, k) for k in vars(cfg)},
        "fleet_events": [{"ts": fmt_time(r["ts"]), "end": fmt_time(r["end"]),
                          "kind": r["kind"], "logins": r["logins"],
                          "reboots": r["reboots"]} for r in scan.restarts],
        "units": units_out,
        "cross_tabs": tabs,
        "anomalies": anomalies,
    }
    with open(outdir / "summary.json", "w") as f:
        json.dump(summary, f, indent=1)
    return summary


def make_plots(outdir, scan, soc, cfg):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    pdir = Path(outdir) / "plots"
    pdir.mkdir(parents=True, exist_ok=True)
    end = scan.last
    mode_color = {MODE_ACTIVE: "#fdd", MODE_IDLE: "#dfd", MODE_OVERNIGHT: "#ddf"}
    for gid, u in sorted(scan.units.items()):
        if not u.readings:
            continue
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 7), sharex=True,
                                       height_ratios=[2, 1])
        for t0, t1, mode in u.segments(end):
            for ax in (ax1, ax2):
                ax.axvspan(t0, t1, color=mode_color.get(mode, "#eee"),
                           alpha=0.5, lw=0)
        rest = [(ts, v) for ts, v, _s, g, a in u.readings
                if g is False and a is False]
        load = [(ts, v) for ts, v, _s, g, a in u.readings
                if not (g is False and a is False)]
        if rest:
            ax1.plot([t for t, _ in rest], [v for _, v in rest], ".-",
                     ms=2, lw=0.6, color="#036", label="V (rest)")
        if load:
            ax1.plot([t for t, _ in load], [v for _, v in load], ".",
                     ms=2, color="#c60", label="V (load)")
        for iv in u.charge_iv:
            ax1.axvspan(iv[0], iv[1], color="#ff0", alpha=0.3, lw=0)
        for a in u.anomalies:
            if a["category"] in ("excess-drain", "voltage-drop"):
                ax1.axvspan(a["t0"], a["t1"], color="red", alpha=0.15, lw=0)
        ax1.set_ylabel("V")
        ax1.legend(loc="upper right", fontsize=7)
        ax1.set_title(f"{gid}  {u.fw or '?'}  {soc.cap_class(gid)}")
        if u.minute_lag:
            ax2.plot([t for t, _m, _x, _n in u.minute_lag],
                     [x for _t, _m, x, _n in u.minute_lag], ".",
                     ms=2, color="#606")
            ax2.set_yscale("symlog")
        ax2.set_ylabel("lag s (max/min.)")
        ticks = ax2.get_xticks()
        ax2.set_xticks(ticks)
        ax2.set_xticklabels([_hm(t) for t in ticks], rotation=30, fontsize=7)
        fig.tight_layout()
        fig.savefig(pdir / f"{gid}.png", dpi=90)
        plt.close(fig)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfiles", nargs="+")
    ap.add_argument("--out", default=None, help="output directory")
    ap.add_argument("--baseline", default=None,
                    help="per-unit baseline JSON from a --baseline-out run")
    ap.add_argument("--baseline-out", default=None,
                    help="write per-unit baseline JSON and exit (no report)")
    ap.add_argument("--plots", action="store_true")
    ap.add_argument("--start", default=None,
                    help="analysis window start (epoch or 'YYYY-MM-DD HH:MM'); "
                         "identity is tracked outside the window, events are not")
    ap.add_argument("--end", default=None, help="analysis window end")
    ap.add_argument("--lag", dest="lag", type=float, default=30.0)
    ap.add_argument("--live", type=float, default=5.0)
    ap.add_argument("--restore", type=float, default=8.0)
    ap.add_argument("--replay-silence", type=float, default=120.0)
    ap.add_argument("--drained-hold", type=float, default=60.0,
                    help="lag must stay low this long to call a replay drained")
    ap.add_argument("--gap", type=float, default=300.0)
    ap.add_argument("--active-secs", type=float, default=30.0)
    ap.add_argument("--active-run", type=float, default=120.0)
    ap.add_argument("--session-gap", type=float, default=1800.0)
    ap.add_argument("--restart-window", type=float, default=60.0)
    ap.add_argument("--restart-min", type=int, default=4)
    ap.add_argument("--idle-grace", type=float, default=120.0)
    ap.add_argument("--t2-min", type=float, default=1500.0)
    ap.add_argument("--idle-loc-min", type=int, default=4,
                    help="min LOC in an idle episode to report (default 4)")
    ap.add_argument("--drain-window-sec", type=float, default=7200.0)
    ap.add_argument("--idle-drain-window-sec", type=float, default=43200.0)
    ap.add_argument("--idle-drain-min-sec", type=float, default=14400.0)
    ap.add_argument("--drain-min-sec", type=float, default=1800.0)
    ap.add_argument("--drain-factor", type=float, default=1.5)
    ap.add_argument("--drain-abs", type=float, default=0.4)
    ap.add_argument("--drop-mv", type=float, default=60.0)
    ap.add_argument("--drop-pair-sec", type=float, default=900.0)
    ap.add_argument("--plag-sec", type=float, default=10.0)
    ap.add_argument("--plag-min-minutes", type=int, default=15)
    ap.add_argument("--users", default=str(Path(__file__).resolve().parent.parent
                                           / "users.json"))
    args = ap.parse_args()
    cfg = argparse.Namespace(
        lag=args.lag, live=args.live, restore=args.restore,
        replay_silence=args.replay_silence, drained_hold=args.drained_hold,
        gap=args.gap,
        active_secs=args.active_secs, active_run=args.active_run,
        session_gap=args.session_gap, restart_window=args.restart_window,
        restart_min=args.restart_min, idle_grace=args.idle_grace,
        t2_min=args.t2_min, idle_loc_min=args.idle_loc_min,
        drain_window_sec=args.drain_window_sec, drain_min_sec=args.drain_min_sec,
        idle_drain_window_sec=args.idle_drain_window_sec,
        idle_drain_min_sec=args.idle_drain_min_sec,
        drain_factor=args.drain_factor, drain_abs=args.drain_abs,
        drop_mv=args.drop_mv, drop_pair_sec=args.drop_pair_sec,
        plag_sec=args.plag_sec, plag_min_minutes=args.plag_min_minutes,
        win0=parse_when(args.start), win1=parse_when(args.end))

    soc = SocModel()
    ok, _errs = soc.self_test()
    if not ok:
        raise SystemExit("SoC model failed anchor self-test")

    scan = FleetScan(cfg, soc)
    scan.run([Path(p) for p in args.logfiles])
    print(f"scanned {len(args.logfiles)} file(s): {len(scan.units)} units, "
          f"{fmt_time(scan.first)} .. {fmt_time(scan.last)}", file=sys.stderr)

    if args.baseline_out:
        base = {}
        for gid, u in sorted(scan.units.items()):
            segs = u.segments(scan.last)
            _anoms, dstats = drain_windows(u, segs, soc, cfg)
            idle_h = u.mode_hours(scan.last)[MODE_IDLE]
            base[gid] = {
                "idle_drain_pct_h": round(statistics.median(dstats[MODE_IDLE]), 3)
                if dstats[MODE_IDLE] else None,
                "idle_windows": len(dstats[MODE_IDLE]),
                "idle_hours": round(idle_h, 1),
                "idle_loc_eps": len([e for e in u.idle_eps if e["n"] >= cfg.idle_loc_min]),
            }
        with open(args.baseline_out, "w") as f:
            json.dump({"window": [fmt_time(scan.first), fmt_time(scan.last)],
                       "units": base}, f, indent=1)
        print(f"baseline for {len(base)} units -> {args.baseline_out}",
              file=sys.stderr)
        return

    baseline = None
    if args.baseline:
        baseline = json.load(open(args.baseline)).get("units", {})

    names = load_users(args.users)
    units_out, anomalies = build_results(scan, soc, cfg, baseline, names)
    tabs = cross_tabs(units_out)

    if args.out:
        write_outputs(args.out, scan, units_out, anomalies, tabs, cfg, args)
        if args.plots:
            make_plots(args.out, scan, soc, cfg)
        print(f"wrote {args.out}/units.csv, anomalies.csv "
              f"({len(anomalies)} anomalies), summary.json", file=sys.stderr)

    # console fleet summary
    print(f"\n{'group':14s}{'units':>6s}{'act_h':>8s}{'idle_h':>8s}  "
          f"anomaly rates /10h (by category)")
    for key in sorted(tabs):
        t = tabs[key]
        rates = "  ".join(f"{c}={r}" for c, r in sorted(t["rates_per_10h"].items()))
        print(f"{key:14s}{t['units']:6d}{t['hours_active']:8.1f}"
              f"{t['hours_idle']:8.1f}  {rates}")
    print("\nfleet events:")
    for r in scan.restarts:
        print(f"  {fmt_time(r['ts'])}  {r['kind']:18s} {r['logins']} logins, "
              f"{r['reboots']} device reboots")


if __name__ == "__main__":
    main()
