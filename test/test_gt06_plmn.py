"""Management contract for the persistent carrier restriction."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_GT06 import GT06Connection, GT06Listener, _p_cellstat, _v_plmn  # noqa: E402


class FakeSock:
    def fileno(self):
        return 1

    def sendall(self, data):
        pass


def listener_and_conn():
    listener = GT06Listener(
        port=None, interval=1, id_prefix="G", get_tracker_func=lambda eid: None,
        gt06_config={"default_eid": 1, "devices": {}}, gt06_config_path=None,
        log_func=lambda *args, **kwargs: None,
    )
    conn = GT06Connection(FakeSock(), ("127.0.0.1", 1))
    conn.imei = "888800000000178"
    conn.firmware = "WSTR-STD1_test"
    conn.authenticated = True
    listener.connections[1] = conn
    return listener, conn


@pytest.mark.parametrize("value", ["", "50501", "50501,50502", "310260,50501",
                                        "111111,222222,333333,444444"])
def test_valid_list(value):
    assert _v_plmn(value) is None


@pytest.mark.parametrize("value", [None, 1, "505", "5050100", "50501,", ",50501",
                                        "50501,,50502", "50501,50501", "50501, 50502",
                                        "５０５０１", "50501#RESET#", "50501\n",
                                        "111111,222222,333333,444444,555555"])
def test_invalid_list(value):
    assert _v_plmn(value)


def test_empty_list_readback_is_editable_as_empty():
    assert _p_cellstat("READOK:CELLSTAT reg5 plmn50502 man0 okany policy1 mode0") == {"PLMNOK": ""}


def test_edit_requires_capability_on_current_connection():
    listener, conn = listener_and_conn()
    row = lambda: next(p for p in listener.params_get(conn.imei)["params"] if p["key"] == "PLMNOK")
    listener._params_parse(conn, "READOK:CELLSTAT reg5 plmn50502 man0 ok50501,50502")
    assert not row()["writable"]
    assert not listener.params_set(conn.imei, "PLMNOK", "50501")[0]
    assert conn.imei not in listener.param_ops

    listener._params_parse(conn, "READOK:CELLSTAT reg5 plmn50502 man1 ok50501,50502 policy1 mode1")
    assert row()["writable"]
    assert not row()["volatile"]
    ok, error = listener.params_set(conn.imei, "PLMNOK", "50501")
    assert ok, error
    assert conn.cmd_pending == "SZCS#PLMNOK=50501"
    op = listener.param_ops[conn.imei]
    assert op["outstanding"] == {"PLMNOK"}
    listener._params_parse(conn, "READOK:CELLSTAT reg5 plmn50502 man1 ok50501 policy1 mode1", op["token"])
    assert not op["outstanding"]

    # A new connection may have different firmware; stale cached values cannot grant write support.
    replacement = GT06Connection(FakeSock(), ("127.0.0.1", 2))
    replacement.imei, replacement.firmware = conn.imei, conn.firmware
    replacement.authenticated = True
    listener.connections[1] = replacement
    assert not row()["writable"]


def test_invalid_set_cannot_queue_commands_or_claim_operation():
    listener, conn = listener_and_conn()
    conn.plmn_policy_supported = True
    assert not listener.params_set(conn.imei, "PLMNOK", "50501#RESET#")[0]
    assert conn.cmd_pending is None
    assert conn.imei not in listener.param_ops
