#!/usr/bin/env python3
"""Shared GT06 battery SoC model.

One place for the parametric OCV->SoC curve and the load-aware voltage
correction that several analysis scripts previously each carried a private
copy of. Data comes from:

  gt06/battery_data/soc_fit.json   -- fitted curve coeffs, per-unit divider
                                      offsets, per-class internal resistance
  WebUI/gt06_calibration.json      -- per-unit capacity / cap class, mode
                                      power figures (track/idle)

A terminal-voltage reading is corrected to cell OCV before the curve:

  OCV = raw_v + offsets_mv[gid]/1000 + I_load * class_r_ohm[cap_class]

Run this file directly for a self-test against the fit's anchor voltages.
"""
import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
DEFAULT_FIT = _REPO / "gt06" / "battery_data" / "soc_fit.json"
DEFAULT_CAL = _REPO / "WebUI" / "gt06_calibration.json"


def soc_model(v, c1, c2, c3, c4):
    """Fitted resting-voltage -> SoC%% curve, clamped to [0, 100]."""
    s = c1 * (1.0 - 1.0 / (1.0 + (v / c2) ** c4) ** c3)
    return max(0.0, min(100.0, s))


def gid_of(imei_or_id, prefix="G"):
    """Normalise an IMEI / login id / G-label to the G<last6> unit label."""
    digits = re.sub(r"\D", "", str(imei_or_id))
    return prefix + digits[-6:] if digits else None


class SocModel:
    """Load-aware SoC model for the GT06 fleet."""

    def __init__(self, fit_path=DEFAULT_FIT, cal_path=DEFAULT_CAL):
        fit = json.load(open(fit_path))
        cal = json.load(open(cal_path))
        self.coeffs = fit["coeffs"]
        self.offsets_mv = fit.get("offsets_mv", {})
        self.class_r_ohm = fit.get("class_r_ohm", {})
        self.anchors_v = {int(k): v for k, v in fit.get("anchors_v", {}).items()}
        self.units = cal.get("units", {})
        self.defaults = cal.get("defaults", {})
        self.track_current_ma = cal.get("track_current_ma", 115.0)
        self.nominal_voltage = cal.get("nominal_voltage", 3.68)
        mp = cal.get("mode_power_w", {})
        self.idle_current_ma = 1000.0 * mp.get("idle", 0.018) / self.nominal_voltage
        self.mode_power_w = mp

    def unit(self, gid):
        return self.units.get(gid, self.defaults)

    def cap_class(self, gid):
        return self.unit(gid).get("cap_class", self.defaults.get("cap_class", "6Ah"))

    def capacity_mah(self, gid):
        return self.unit(gid).get("capacity_mah",
                                  self.defaults.get("capacity_mah", 6000))

    def load_ma(self, mode):
        """Nominal load current for a mode ('track'/'idle'/'rest')."""
        if mode == "track":
            return self.track_current_ma
        if mode == "idle":
            return self.idle_current_ma
        return 0.0

    def ocv(self, gid, raw_v, load_ma=0.0):
        """Terminal voltage -> cell OCV (per-unit divider offset + IR add-back)."""
        off = self.offsets_mv.get(gid, 0.0) / 1000.0
        r = self.class_r_ohm.get(self.cap_class(gid), 0.5)
        return raw_v + off + (load_ma / 1000.0) * r

    def soc(self, ocv_v):
        c = self.coeffs
        return soc_model(ocv_v, c["c1"], c["c2"], c["c3"], c["c4"])

    def soc_of(self, gid, raw_v, mode="rest"):
        """SoC%% from a raw terminal-voltage reading taken under `mode` load."""
        return self.soc(self.ocv(gid, raw_v, self.load_ma(mode)))

    def expected_drain_pct_h(self, gid, mode):
        """Model-expected SoC%%/h burn for a unit in a mode."""
        return self.load_ma(mode) / self.capacity_mah(gid) * 100.0

    def self_test(self):
        """Check the curve reproduces the fit's anchor voltages (<1% SoC off)."""
        errs = {}
        for pct, v in self.anchors_v.items():
            errs[pct] = self.soc(v) - pct
        worst = max(abs(e) for e in errs.values()) if errs else float("inf")
        return worst < 1.0, errs


if __name__ == "__main__":
    m = SocModel()
    ok, errs = m.self_test()
    for pct in sorted(errs):
        print(f"anchor {pct:2d}% -> {m.anchors_v[pct]:.4f}V : "
              f"model {pct + errs[pct]:6.2f}%  (err {errs[pct]:+.2f}%)")
    print(f"idle load {m.idle_current_ma:.1f} mA, track {m.track_current_ma:.0f} mA")
    for gid, mode in (("G226122", "idle"), ("G226122", "track"),
                      ("G187547", "idle"), ("G187547", "track")):
        print(f"{gid} {mode}: expected {m.expected_drain_pct_h(gid, mode):.2f}%/h "
              f"({m.cap_class(gid)}, {m.capacity_mah(gid)} mAh)")
    print("SELF-TEST", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)
