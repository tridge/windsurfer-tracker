"""Unit tests for the parametric (OCV-fit) battery % — GT06Listener._battery_percent.

bat% = SoC(OCV), OCV = V + soc_fit.offsets_mv[id]/1000 + I_load*class_r_ohm[cap_class],
I_load = track_current_ma (active) or mode_power_w.idle/nominal_voltage (idle). Replaces
the retired single-cell (G226122) discharge table. Mirrors WebUI/js/battery_cal.js.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_GT06 import GT06Listener  # noqa: E402

CAL = Path(__file__).resolve().parent.parent / "WebUI" / "gt06_calibration.json"


def _listener():
    return GT06Listener(0, 1, "G", lambda *a, **k: None,
                        gt06_config={"default_eid": 1, "devices": {}},
                        log_func=lambda *a, **k: None, battery_cal_path=CAL)


def _conn(sailor_id, idle):
    return SimpleNamespace(sailor_id=sailor_id, idle=idle)


def test_calibration_loaded():
    lst = _listener()
    assert lst.battery_cal.get("soc_fit"), "soc_fit must load from gt06_calibration.json"
    assert "discharge_curve" not in lst.battery_cal  # legacy table removed


def test_g347082_load_corrected_values():
    # G347082 (6Ah): idle 4.04V ~= 86%, tracking 3.77V ~= 64% (load-corrected real SoC).
    lst = _listener()
    assert abs(lst._battery_percent(_conn("G347082", True), 4.04) - 86) <= 1
    assert abs(lst._battery_percent(_conn("G347082", False), 3.77) - 64) <= 1


def test_track_reads_higher_at_same_terminal_v():
    # Same terminal voltage: tracking sags ~57mV more, so the IR add-back lifts its
    # OCV higher -> it reads higher % than an idle reading at the same terminal V.
    # (Equivalently: at the same real charge, tracking reports a lower terminal V.)
    lst = _listener()
    idle = lst._battery_percent(_conn("G347082", True), 3.85)
    trk = lst._battery_percent(_conn("G347082", False), 3.85)
    assert trk > idle


def test_monotonic_in_voltage():
    lst = _listener()
    pcts = [lst._battery_percent(_conn("G347082", False), v)
            for v in (3.6, 3.7, 3.8, 3.9, 4.0, 4.1)]
    assert pcts == sorted(pcts)
    assert pcts[0] >= 0 and pcts[-1] <= 100


def test_unknown_unit_uses_defaults():
    lst = _listener()
    p = lst._battery_percent(_conn("G999999", False), 3.90)
    assert 0 <= p <= 100


def test_no_voltage_is_unknown():
    lst = _listener()
    assert lst._battery_percent(_conn("G347082", False), None) == -1


def test_missing_calibration_is_unknown():
    lst = GT06Listener(0, 1, "G", lambda *a, **k: None,
                       gt06_config={"default_eid": 1, "devices": {}},
                       log_func=lambda *a, **k: None, battery_cal_path=None)
    assert lst._battery_percent(_conn("G347082", False), 3.77) == -1
