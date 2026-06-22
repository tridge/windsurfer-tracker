"""Unit tests for night-idle: the sleep-schedule window switches idle units to
long MODE1 idle intervals (instead of MODE5 deep-sleep) when night_idle.enabled.

Covers:
- load_gt06_config: night_idle defaults + merge + acc_off clamp.
- clean_night_idle: bounds/type validation.
- GT06Listener._idle_intervals: per-connection day vs night resolution via the
  get_event_night_active callback.
- event_sleep_window_active is gated OFF when night_idle is enabled (the window
  then drives night-idle, not deep-sleep); event_night_idle_active reflects it.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import tracker_server  # noqa: E402
from protocol_GT06 import GT06Listener, load_gt06_config, DEFAULT_NIGHT_IDLE  # noqa: E402
from tracker_server import clean_night_idle  # noqa: E402


def _load(tmp_path, cfg):
    p = tmp_path / "gt06.json"
    import json
    p.write_text(json.dumps(cfg))
    return load_gt06_config(p, log_func=lambda *_: None)


def _listener(night_idle=None, sleep_schedule=None):
    cfg = {"default_eid": 1, "devices": {}}
    if night_idle is not None:
        cfg["night_idle"] = night_idle
    if sleep_schedule is not None:
        cfg["sleep_schedule"] = sleep_schedule
    return GT06Listener(0, 1, "G", lambda *a, **k: None, gt06_config=cfg,
                        log_func=lambda *a, **k: None)


class _Conn:
    def __init__(self, eid=1):
        self.eid = eid


# ---- load_gt06_config -------------------------------------------------------

def test_night_idle_defaults_present(tmp_path):
    loaded = _load(tmp_path, {"default_eid": 1, "devices": {}})
    assert loaded["night_idle"] == DEFAULT_NIGHT_IDLE
    assert loaded["night_idle"]["enabled"] is False  # opt-in


def test_night_idle_partial_merge(tmp_path):
    loaded = _load(tmp_path, {"default_eid": 1, "devices": {},
                              "night_idle": {"enabled": True, "hbt_interval": 1200}})
    ni = loaded["night_idle"]
    assert ni["enabled"] is True and ni["hbt_interval"] == 1200
    # unspecified keys keep their defaults
    assert ni["keepalive_interval"] == DEFAULT_NIGHT_IDLE["keepalive_interval"]
    assert ni["cxzt_poll_min"] == DEFAULT_NIGHT_IDLE["cxzt_poll_min"]


def test_night_idle_acc_off_clamped(tmp_path):
    loaded = _load(tmp_path, {"default_eid": 1, "devices": {},
                              "night_idle": {"acc_off_interval": 99999}})
    lst = _listener(night_idle=loaded["night_idle"])
    assert lst.night_idle["acc_off_interval"] == 1800  # firmware T2 max


# ---- clean_night_idle -------------------------------------------------------

def test_clean_night_idle_coerces_and_passes():
    out = clean_night_idle({"enabled": True, "hbt_interval": "900",
                            "keepalive_interval": 1800, "cxzt_poll_min": 30,
                            "acc_off_interval": 1800, "gps_rst_time": 60})
    assert out["enabled"] is True and out["hbt_interval"] == 900


def test_clean_night_idle_rejects_bad_enabled():
    with pytest.raises(ValueError):
        clean_night_idle({"enabled": "yes"})


def test_clean_night_idle_rejects_out_of_range():
    with pytest.raises(ValueError):
        clean_night_idle({"acc_off_interval": 5000})   # > 1800 firmware max
    with pytest.raises(ValueError):
        clean_night_idle({"hbt_interval": 0})          # < 5
    with pytest.raises(ValueError):
        clean_night_idle({"cxzt_poll_min": -1})


def test_clean_night_idle_only_present_keys():
    assert clean_night_idle({"cxzt_poll_min": 15}) == {"cxzt_poll_min": 15}


# ---- _idle_intervals (per-connection day vs night) --------------------------

def test_idle_intervals_day_when_not_night():
    lst = _listener(night_idle={"enabled": True, "hbt_interval": 900,
                                "keepalive_interval": 1800, "cxzt_poll_min": 30})
    lst.get_event_night_active = lambda eid: False
    eff = lst._idle_intervals(_Conn())
    assert eff["night"] is False
    assert eff["hbt"] == lst.idle_hbt_interval          # day (15)
    assert eff["keepalive"] == lst.idle_keepalive_interval
    assert eff["cxzt_min"] == lst.cxzt_poll_min


def test_idle_intervals_night_when_active():
    lst = _listener(night_idle={"enabled": True, "hbt_interval": 900,
                                "keepalive_interval": 1800, "cxzt_poll_min": 30,
                                "acc_off_interval": 1800, "gps_rst_time": 60})
    lst.get_event_night_active = lambda eid: True
    eff = lst._idle_intervals(_Conn())
    assert eff["night"] is True
    assert eff["hbt"] == 900 and eff["keepalive"] == 1800 and eff["cxzt_min"] == 30


def test_idle_intervals_day_when_feature_disabled():
    # night_idle.enabled False -> always day, even if the callback says active.
    lst = _listener(night_idle={"enabled": False, "hbt_interval": 900})
    lst.get_event_night_active = lambda eid: True
    eff = lst._idle_intervals(_Conn())
    assert eff["night"] is False and eff["hbt"] == lst.idle_hbt_interval


def test_idle_intervals_callback_error_falls_to_day():
    lst = _listener(night_idle={"enabled": True, "hbt_interval": 900})
    def boom(eid):
        raise RuntimeError("nope")
    lst.get_event_night_active = boom
    eff = lst._idle_intervals(_Conn())
    assert eff["night"] is False  # robust: never crash the idle path


# ---- schedule gating (event_sleep_window_active vs night-idle) --------------

class _EvMgr:
    def __init__(self, ev):
        self._ev = ev
    def get_event(self, eid):
        return self._ev
    def list_events(self):
        return [1]


def _enclosing_window(tz="UTC"):
    """A ±1h HH:MM window guaranteed to contain 'now' in tz (wrap-safe)."""
    now = datetime.now(ZoneInfo(tz))
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    return start, end


def _wire(monkeypatch, night_enabled, in_window=True):
    start, end = _enclosing_window() if in_window else ("00:00", "00:00")
    sched = {"enabled": True, "start": start, "end": end,
             "overnight_mode_number": 5, "overnight_interval_min": 60}
    lst = _listener(night_idle={"enabled": night_enabled}, sleep_schedule=sched)
    monkeypatch.setattr(tracker_server, "_gt06_listener", lst)
    monkeypatch.setattr(tracker_server, "_event_manager",
                        _EvMgr({"timezone": "UTC"}))
    return lst


def test_mode5_window_gated_off_when_night_idle_enabled(monkeypatch):
    _wire(monkeypatch, night_enabled=True, in_window=True)
    # In-window, but night_idle on -> MODE5 path is disabled...
    assert tracker_server.event_sleep_window_active(1) is None
    # ...and night-idle is active instead.
    assert tracker_server.event_night_idle_active(1) is True


def test_mode5_window_active_when_night_idle_disabled(monkeypatch):
    _wire(monkeypatch, night_enabled=False, in_window=True)
    params = tracker_server.event_sleep_window_active(1)
    assert params is not None and params[0] == 5      # (mode, interval)
    assert tracker_server.event_night_idle_active(1) is False


def test_night_idle_inactive_outside_window(monkeypatch):
    _wire(monkeypatch, night_enabled=True, in_window=False)
    assert tracker_server.event_night_idle_active(1) is False
