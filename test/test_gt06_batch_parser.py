"""Unit tests for the batched-LOC (0xF1) wire parser.

These are pure-function tests on hostile input: the parser sees bytes off the network before the
listener has finished deciding who the peer is, so malformed frames must be rejected rather than
producing a plausible-looking position.
"""

import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_GT06 import (  # noqa: E402
    gt06_parse_batch, GT06_HDOP_TO_METRES, GT06_HACC_SATURATED_M,
    GT06_BATCH_MAX_SPAN_S, GT06_BATCH_MAX_FUTURE_S,
)


def _hdr(count=1, sats=17, fix=3, hdop_x10=12,
         dt=(26, 8, 20, 1, 2, 3), base_ds=4, fmt=1):
    return bytes([fmt, count, sats, fix, hdop_x10]) + bytes(dt) + bytes([base_ds])


def _elem(dt_ds=0, lat=-35.0, lon=149.0, spd_dkn=60, hdg=45):
    return struct.pack(">HiiHH", dt_ds, int(lat * 1e7), int(lon * 1e7), spd_dkn, hdg)


def test_parses_a_well_formed_batch():
    b = gt06_parse_batch(_hdr(count=2) + _elem(0) + _elem(15, lat=-35.001))
    assert b is not None
    assert b["satellites"] == 17 and b["fix_type"] == 3
    assert b["hdop"] == pytest.approx(1.2)
    assert b["horizontal_accuracy"] == pytest.approx(1.2 * GT06_HDOP_TO_METRES, abs=0.05)
    assert len(b["fixes"]) == 2
    # 0.1 s resolution: base ds .4 plus a 1.5 s offset
    assert b["fixes"][1]["ts"] - b["fixes"][0]["ts"] == pytest.approx(1.5, abs=0.001)
    assert b["fixes"][0]["speed_knots"] == pytest.approx(6.0)
    assert b["fixes"][0]["heading"] == 45


def test_satellite_count_uses_all_eight_bits():
    b = gt06_parse_batch(_hdr(count=1, sats=200) + _elem())
    assert b["satellites"] == 200


@pytest.mark.parametrize("payload,why", [
    (b"", "empty"),
    (b"\x01\x01", "header truncated"),
    (_hdr(count=1), "header only, element missing"),
    (_hdr(count=2) + _elem(), "count claims more elements than present"),
    (_hdr(count=0) + _elem(), "zero count"),
    (_hdr(count=255) + _elem(), "absurd count"),
    (_hdr(fmt=2) + _elem(), "unknown format version"),
])
def test_malformed_payloads_are_rejected(payload, why):
    assert gt06_parse_batch(payload) is None, f"accepted a bad payload: {why}"


def test_out_of_range_element_is_dropped_not_the_whole_batch():
    good = _elem(0, lat=-35.0, lon=149.0)
    bad = struct.pack(">HiiHH", 10, int(95.0 * 1e7), 0, 0, 0)   # latitude 95 deg
    b = gt06_parse_batch(_hdr(count=2) + good + bad)
    assert b is not None and len(b["fixes"]) == 1
    assert b["fixes"][0]["lat"] == pytest.approx(-35.0)


def test_all_elements_bad_rejects_the_batch():
    bad = struct.pack(">HiiHH", 0, int(95.0 * 1e7), 0, 0, 0)
    assert gt06_parse_batch(_hdr(count=1) + bad) is None


@pytest.mark.parametrize("dt", [
    (26, 99, 20, 1, 2, 3),    # month 99
    (26, 8, 99, 1, 2, 3),     # day 99
    (26, 8, 20, 99, 2, 3),    # hour 99
    (26, 8, 20, 1, 99, 3),    # minute 99
    (26, 0, 0, 0, 0, 0),      # month/day zero
])
def test_impossible_datetime_falls_back_to_receipt_time(dt):
    """timegm() normalises hour 99 into a wildly wrong date instead of raising, so the parser has to
    range-check. A bogus stamp must not become a plausible timestamp far from now."""
    b = gt06_parse_batch(_hdr(count=1, dt=dt) + _elem())
    assert b is not None
    assert abs(b["fixes"][0]["ts"] - time.time()) < 5, "bogus datetime was trusted"


def test_saturated_hdop_maps_to_an_unambiguously_bad_accuracy():
    """255 means 'worse than 25.4', not 25.5 — it must exceed the UI's 100 m BADGPS threshold rather
    than saturating at a plausible-looking ~64 m."""
    b = gt06_parse_batch(_hdr(count=1, hdop_x10=255) + _elem())
    assert b["hdop_saturated"] is True
    assert b["horizontal_accuracy"] == GT06_HACC_SATURATED_M
    assert b["horizontal_accuracy"] > 100


def test_unknown_hdop_reports_no_accuracy():
    b = gt06_parse_batch(_hdr(count=1, hdop_x10=0) + _elem())
    assert b["hdop"] is None and b["horizontal_accuracy"] is None


def test_absurd_delta_is_rejected_not_trusted():
    """dt_ds is 16 bits, so a bad frame can claim +6553.5s. The firmware caps a batch at 120s; the
    parser must enforce that invariant rather than publishing a timestamp in the future, which would
    suppress genuine updates until wall time caught up."""
    good = _elem(0)
    absurd = _elem(65535)
    b = gt06_parse_batch(_hdr(count=2) + good + absurd)
    assert b is not None and len(b["fixes"]) == 1, "absurd delta was accepted"
    assert b["fixes"][0]["ts"] < time.time() + GT06_BATCH_MAX_SPAN_S + 5


def test_future_timestamp_cannot_be_synthesised_from_a_bogus_base():
    """Invalid base falls back to receipt time; an unchecked delta on top of that still produced a
    future timestamp before the span bound was added."""
    b = gt06_parse_batch(_hdr(count=1, dt=(26, 99, 20, 1, 2, 3)) + _elem(65535))
    assert b is None or b["fixes"][0]["ts"] <= time.time() + GT06_BATCH_MAX_SPAN_S + 5


def test_out_of_order_elements_are_sorted():
    """The dispatch publishes fixes[-1]; an out-of-order batch must not rewind the live position."""
    b = gt06_parse_batch(_hdr(count=3) + _elem(0) + _elem(50) + _elem(40))
    assert b is not None
    tss = [f["ts"] for f in b["fixes"]]
    assert tss == sorted(tss), f"parser left the batch out of order: {tss}"


def _sod_now_dt():
    """A header datetime for 'now', so span tests are not fighting the receipt-time anchor."""
    t = time.gmtime()
    return (t.tm_year - 2000, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


@pytest.mark.parametrize("dt_ds,keep", [
    (0, True),
    (1200, True),       # exactly the firmware's 120 s cap
    (1201, False),      # one decisecond past it
    (3000, False),      # the old 300 s slack: five minutes of frozen track
])
def test_span_bound_matches_the_firmware_invariant(dt_ds, keep):
    """The server must enforce the producer's 120 s cap, not a looser one — a future timestamp
    advances the duplicate high-water mark and suppresses real updates until wall time catches up."""
    b = gt06_parse_batch(_hdr(count=2, dt=_sod_now_dt()) + _elem(0) + _elem(dt_ds))
    assert b is not None
    assert len(b["fixes"]) == (2 if keep else 1), \
        f"dt_ds={dt_ds} should {'be kept' if keep else 'be dropped'}"


def test_far_future_base_is_anchored_to_receipt_time():
    """A syntactically valid but absurd date passes every field check; unanchored it would freeze the
    live track for as long as it claims (a 2099 base = decades)."""
    b = gt06_parse_batch(_hdr(count=1, dt=(99, 6, 1, 12, 0, 0)) + _elem(0))
    assert b is not None
    assert b["fixes"][0]["ts"] <= time.time() + GT06_BATCH_MAX_FUTURE_S + 5, \
        "a year-2099 base was trusted"


def test_ancient_base_is_anchored_to_receipt_time():
    b = gt06_parse_batch(_hdr(count=1, dt=(1, 1, 1, 0, 0, 0)) + _elem(0))
    assert b is not None
    assert b["fixes"][0]["ts"] > time.time() - 3600, "a year-2001 base was trusted"
