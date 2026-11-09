"""When a WSTR unit's target has night sleep off (a start, or the morning wake), the reconcile
must turn sleep off before the read-back, so one slow ACK can't cost a whole wake interval."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_GT06 import GT06Connection, GT06Listener  # noqa: E402


class FakeSock:
    def __init__(self):
        self.sent = []

    def fileno(self):
        return 1

    def sendall(self, data):
        self.sent.append(data)


def first_commands(firmware, state, night=False):
    listener = GT06Listener(
        port=None, interval=1, id_prefix="G", get_tracker_func=lambda eid: None,
        gt06_config={"default_eid": 1, "devices": {}}, gt06_config_path=None,
        log_func=lambda *args, **kwargs: None,
        get_event_night_active_func=lambda eid: night,
    )
    conn = GT06Connection(FakeSock(), ("127.0.0.1", 1))
    conn.imei, conn.firmware, conn.authenticated = "888800000000179", firmware, True
    listener.connections[1] = conn
    listener._reconcile_begin(conn, state)
    sent = conn.sock.sent[0].decode("latin-1")
    return sent, [c for c, _ in conn.cmd_queue]


@pytest.mark.parametrize("state", ["active", "idle"])   # a start, and the morning wake to day idle
def test_wstr_disables_sleep_first(state):
    sent, queue = first_commands("WSTR-STD1_test", state)
    assert "SZCS#SLPDISCONNECT=0" in sent
    assert queue[0] == "cxzt#"


@pytest.mark.parametrize("firmware,state,night", [("W07_MG133_10F8G_B53_V667", "active", False),
                                                  (None, "active", False),
                                                  ("WSTR-STD1_test", "idle", True)])  # sleep wanted
def test_others_start_with_cxzt(firmware, state, night):
    sent, queue = first_commands(firmware, state, night)
    assert "cxzt#" in sent
    assert "SZCS#SLPDISCONNECT" not in sent and "SZCS#SLPDISCONNECT=0" not in queue
