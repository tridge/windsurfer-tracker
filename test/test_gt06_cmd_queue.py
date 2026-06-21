"""Unit tests for the GT06 command queue / reconcile pacing.

No server/socket needed — drives GT06Listener._send_next_cmd directly with a fake
socket. Regression for the bug where the query->apply reconcile transition sent two
corrective commands back-to-back without waiting for the first's ACK (and then a
stray ACK marked the reconcile complete early).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_GT06 import GT06Listener, GT06Connection  # noqa: E402


class FakeSock:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def fileno(self):
        return 1

    def close(self):
        pass


def _listener():
    return GT06Listener(
        port=None, interval=1, id_prefix="G",
        get_tracker_func=lambda eid: None,
        gt06_config={"default_eid": 1, "devices": {}, "idle_hbt_interval": 30,
                     "idle_acc_on_interval": 300, "idle_acc_off_interval": 1800},
        gt06_config_path=None,
        log_func=lambda *a, **k: None)


def _idle_conn(lis):
    gt = GT06Connection(FakeSock(), ("1.2.3.4", 5000), conn_id=1)
    gt.sailor_id, gt.imei, gt.eid, gt.idle = "G000001", "999000000000001", 1, True
    # Query phase just drained; observed differs from desired idle in TWO keys,
    # so _reconcile_apply will queue >=2 corrective commands.
    gt.target_state = "idle"
    gt.reconcile_phase = "query"
    desired = lis._desired_settings(gt, "idle")
    gt.observed = dict(desired)
    gt.observed["SENDS"] = 0     # -> SENDS,1#
    gt.observed["HBT"] = 999     # -> HBT,30,30#
    gt.cmd_queue = []
    gt.cmd_pending = None
    return gt


def test_corrective_commands_paced_one_at_a_time():
    lis = _listener()
    gt = _idle_conn(lis)

    # query->apply transition: must send exactly ONE command, leave the rest queued.
    lis._send_next_cmd(gt)
    assert len(gt.sock.sent) == 1, f"expected 1 command sent, got {len(gt.sock.sent)}"
    assert gt.cmd_pending is not None, "cmd_pending not set — gate broken"
    assert len(gt.cmd_queue) >= 1, "2nd corrective command not left queued (was bursted)"

    # Reconcile must NOT be complete while a corrective command is still queued.
    assert gt.reconcile_phase == "apply", "reconcile advanced past apply too early"

    # ACK the first, pump: now the second goes out.
    gt.cmd_pending = None
    lis._send_next_cmd(gt)
    assert len(gt.sock.sent) == 2, "2nd command not sent after 1st ACK"

    # ACK the second, pump: queue empty -> reconcile completes (not before).
    gt.cmd_pending = None
    lis._send_next_cmd(gt)
    assert len(gt.sock.sent) == 2, "extra command sent after queue drained"
    assert gt.reconcile_phase is None, "reconcile not completed after last ACK"
