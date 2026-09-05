"""LTE STATUS parsing and WSTR-only night sleep policy."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from protocol_GT06 import GT06Connection, GT06Listener, _parse_lte_status  # noqa: E402


class FakeSock:
    def fileno(self):
        return 1


def _listener(night):
    return GT06Listener(
        port=None, interval=1, id_prefix="G", get_tracker_func=lambda eid: None,
        get_event_night_active_func=lambda eid: night,
        gt06_config={
            "default_eid": 1,
            "devices": {},
            "idle_hbt_interval": 30,
            "idle_acc_on_interval": 60,
            "idle_acc_off_interval": 1800,
            "night_idle": {
                "enabled": True,
                "hbt_interval": 900,
                "keepalive_interval": 1800,
                "cxzt_poll_min": 30,
                "acc_off_interval": 1800,
                "gps_rst_time": 60,
            },
        },
        gt06_config_path=None, log_func=lambda *args, **kwargs: None,
    )


def test_lte_status_snapshot():
    status = (
        "Battery:4.100V;GPS:OFF;Defense:OFF;"
        "LTE:505-02|EUTRAN-BAND3|52200|36519691|101|-102|-12|-75|8;")
    assert _parse_lte_status(status) == {
        "plmn": "505-02", "carrier": "Optus", "band": "EUTRAN-BAND3",
        "tac": 52200, "cell_id": 36519691, "pci": 101,
        "rsrp": -102, "rsrq": -12, "rssi": -75, "sinr": 8,
    }


def test_lte_status_rejects_malformed_data():
    assert _parse_lte_status("Battery:4.1V;") is None
    assert _parse_lte_status(";LTE:505-02|band|1|2|3|4|5|6;") is None
    assert _parse_lte_status(";LTE:505-02|bad band|1|2|3|4|5|6|7;") is None
    assert _parse_lte_status(";LTE:505-02|B3|1|2|3|four|5|6|7;") is None


def test_night_disconnected_sleep_is_wstr_only():
    lis = _listener(night=True)
    ours = GT06Connection(FakeSock(), ("127.0.0.1", 1))
    ours.eid, ours.firmware = 1, "WSTR-STD1_deadbee"
    vendor = GT06Connection(FakeSock(), ("127.0.0.1", 2))
    vendor.eid, vendor.firmware = 1, "W07_MG133_10F8G_B53_V668"
    assert lis._desired_settings(ours, "idle")["SLPDISCONNECT"] == 1
    assert lis._desired_settings(vendor, "idle")["SLPDISCONNECT"] == 0


def test_wstr_day_idle_stays_connected():
    lis = _listener(night=False)
    ours = GT06Connection(FakeSock(), ("127.0.0.1", 1))
    ours.eid, ours.firmware = 1, "WSTR-STD1_deadbee"
    assert lis._desired_settings(ours, "idle")["SLPDISCONNECT"] == 0
