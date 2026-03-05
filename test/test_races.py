"""Tests for race management API (create, start, finish, DNF, undo, delete)."""

import json
import time

from conftest import create_event


# ── Create race ──────────────────────────────────────────────


def test_create_race(http_client):
    """POST /admin/races should create a new race."""
    eid = create_event(http_client, name="Race Create", admin_password="racecreate")
    status, body = http_client.post(
        f"/api/event/{eid}/admin/races",
        data={"name": "Race 1"},
        headers={"X-Admin-Password": "racecreate"},
    )
    assert status == 200
    assert body["id"] == 1
    assert body["name"] == "Race 1"
    assert body["start_ts"] is None
    assert body["finishers"] == []


def test_create_race_requires_name(http_client):
    """Race creation without a name should fail."""
    eid = create_event(http_client, name="Race NoName", admin_password="racename")
    status, body = http_client.post(
        f"/api/event/{eid}/admin/races",
        data={"name": ""},
        headers={"X-Admin-Password": "racename"},
    )
    assert status == 400


def test_create_race_requires_auth(http_client):
    """Race creation without admin auth should fail."""
    eid = create_event(http_client, name="Race NoAuth", admin_password="racenoauth")
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races",
        data={"name": "Race 1"},
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.20.1.1"},
    )
    assert status == 401


def test_create_multiple_races(http_client):
    """Creating multiple races should auto-increment IDs."""
    eid = create_event(http_client, name="Race Multi", admin_password="racemulti")
    pw = {"X-Admin-Password": "racemulti"}
    _, r1 = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "Race 1"}, headers=pw)
    _, r2 = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "Race 2"}, headers=pw)
    assert r1["id"] == 1
    assert r2["id"] == 2


# ── List races ───────────────────────────────────────────────


def test_list_races_public(http_client):
    """GET /races should be public and list all races."""
    eid = create_event(http_client, name="Race List", admin_password="racelist")
    pw = {"X-Admin-Password": "racelist"}
    http_client.post(f"/api/event/{eid}/admin/races", data={"name": "Race A"}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races", data={"name": "Race B"}, headers=pw)

    # No auth needed
    status, body = http_client.get(f"/api/event/{eid}/races")
    assert status == 200
    assert len(body["races"]) == 2
    assert body["races"][0]["name"] == "Race A"
    assert body["races"][1]["name"] == "Race B"


def test_list_races_empty(http_client):
    """GET /races on fresh event should return empty list."""
    eid = create_event(http_client, name="Race Empty", admin_password="raceempty")
    status, body = http_client.get(f"/api/event/{eid}/races")
    assert status == 200
    assert body["races"] == []


# ── Start race ───────────────────────────────────────────────


def test_start_race(http_client):
    """POST /admin/races/{id}/start should set start time."""
    eid = create_event(http_client, name="Race Start", admin_password="racestart")
    pw = {"X-Admin-Password": "racestart"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    start_ts = time.time()
    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/start",
        data={"start_ts": start_ts},
        headers=pw,
    )
    assert status == 200
    assert abs(body["start_ts"] - start_ts) < 0.01


def test_reset_start_time(http_client):
    """Setting start_ts to null should clear start, end, and all finishers."""
    eid = create_event(http_client, name="Race Reset", admin_password="racereset")
    pw = {"X-Admin-Password": "racereset"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    rid = race['id']

    # Start, end, and record some finishes
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/start", data={"start_ts": time.time()}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/end", data={"end_ts": time.time() + 3600}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/finish", data={"sailor_id": "S01", "finish_ts": time.time() + 100}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/dnf", data={"sailor_id": "S02"}, headers=pw)

    # Reset
    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{rid}/start",
        data={"start_ts": None},
        headers=pw,
    )
    assert status == 200
    assert body["start_ts"] is None
    assert body["end_ts"] is None
    assert body["finishers"] == []


def test_start_nonexistent_race(http_client):
    """Starting a nonexistent race should return 404."""
    eid = create_event(http_client, name="Race Start404", admin_password="racestart404")
    pw = {"X-Admin-Password": "racestart404"}
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/999/start",
        data={"start_ts": time.time()},
        headers=pw,
    )
    assert status == 404


# ── Record finish ────────────────────────────────────────────


def test_record_finish(http_client):
    """POST /admin/races/{id}/finish should record a finish."""
    eid = create_event(http_client, name="Race Finish", admin_password="racefinish")
    pw = {"X-Admin-Password": "racefinish"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/start", data={"start_ts": start_ts}, headers=pw)

    finish_ts = start_ts + 300
    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/finish",
        data={"sailor_id": "S01", "finish_ts": finish_ts},
        headers=pw,
    )
    assert status == 200
    assert len(body["finishers"]) == 1
    assert body["finishers"][0]["sailor_id"] == "S01"
    assert body["finishers"][0]["status"] == "finished"
    assert abs(body["finishers"][0]["finish_ts"] - finish_ts) < 0.01


def test_finish_multiple_sailors(http_client):
    """Multiple sailors can finish in order."""
    eid = create_event(http_client, name="Race MultiFin", admin_password="racemultifin")
    pw = {"X-Admin-Password": "racemultifin"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/start", data={"start_ts": start_ts}, headers=pw)

    for i, sid in enumerate(["S01", "S02", "S03"]):
        status, body = http_client.post(
            f"/api/event/{eid}/admin/races/{race['id']}/finish",
            data={"sailor_id": sid, "finish_ts": start_ts + 300 + i * 10},
            headers=pw,
        )
        assert status == 200

    # Verify order
    status, body = http_client.get(f"/api/event/{eid}/races")
    finishers = body["races"][0]["finishers"]
    assert len(finishers) == 3
    assert finishers[0]["sailor_id"] == "S01"
    assert finishers[1]["sailor_id"] == "S02"
    assert finishers[2]["sailor_id"] == "S03"


def test_duplicate_finish_rejected(http_client):
    """Finishing the same sailor twice should return 400."""
    eid = create_event(http_client, name="Race DupFin", admin_password="racedupfin")
    pw = {"X-Admin-Password": "racedupfin"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/start", data={"start_ts": start_ts}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/finish", data={"sailor_id": "S01", "finish_ts": start_ts + 300}, headers=pw)

    # Second finish for same sailor
    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/finish",
        data={"sailor_id": "S01", "finish_ts": start_ts + 310},
        headers=pw,
    )
    assert status == 400
    assert "already" in body["error"].lower()


def test_finish_requires_sailor_id(http_client):
    """Finish without sailor_id should fail."""
    eid = create_event(http_client, name="Race NoSailor", admin_password="racenosailor")
    pw = {"X-Admin-Password": "racenosailor"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/finish",
        data={"finish_ts": time.time()},
        headers=pw,
    )
    assert status == 400


def test_finish_nonexistent_race(http_client):
    """Finishing in a nonexistent race should return 404."""
    eid = create_event(http_client, name="Race Fin404", admin_password="racefin404")
    pw = {"X-Admin-Password": "racefin404"}
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/999/finish",
        data={"sailor_id": "S01", "finish_ts": time.time()},
        headers=pw,
    )
    assert status == 404


# ── DNF ──────────────────────────────────────────────────────


def test_mark_dnf(http_client):
    """POST /admin/races/{id}/dnf should mark a sailor as DNF."""
    eid = create_event(http_client, name="Race DNF", admin_password="racednf")
    pw = {"X-Admin-Password": "racednf"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/dnf",
        data={"sailor_id": "S05"},
        headers=pw,
    )
    assert status == 200
    assert len(body["finishers"]) == 1
    assert body["finishers"][0]["sailor_id"] == "S05"
    assert body["finishers"][0]["status"] == "dnf"
    assert body["finishers"][0]["finish_ts"] is None


def test_duplicate_dnf_rejected(http_client):
    """DNF for the same sailor twice should return 400."""
    eid = create_event(http_client, name="Race DupDNF", admin_password="racedupndf")
    pw = {"X-Admin-Password": "racedupndf"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/dnf", data={"sailor_id": "S05"}, headers=pw)

    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/dnf",
        data={"sailor_id": "S05"},
        headers=pw,
    )
    assert status == 400


def test_dnf_after_finish_rejected(http_client):
    """DNF for a sailor who already finished should return 400."""
    eid = create_event(http_client, name="Race DNFAfterFin", admin_password="racednfaf")
    pw = {"X-Admin-Password": "racednfaf"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/start", data={"start_ts": start_ts}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/finish", data={"sailor_id": "S01", "finish_ts": start_ts + 300}, headers=pw)

    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/dnf",
        data={"sailor_id": "S01"},
        headers=pw,
    )
    assert status == 400


# ── DNS ──────────────────────────────────────────────────────


def test_mark_dns(http_client):
    """POST /admin/races/{id}/dns should mark a sailor as DNS."""
    eid = create_event(http_client, name="Race DNS", admin_password="racedns")
    pw = {"X-Admin-Password": "racedns"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/dns",
        data={"sailor_id": "S10"},
        headers=pw,
    )
    assert status == 200
    assert len(body["finishers"]) == 1
    assert body["finishers"][0]["sailor_id"] == "S10"
    assert body["finishers"][0]["status"] == "dns"
    assert body["finishers"][0]["finish_ts"] is None


def test_duplicate_dns_rejected(http_client):
    """DNS for the same sailor twice should return 400."""
    eid = create_event(http_client, name="Race DupDNS", admin_password="racedupdns")
    pw = {"X-Admin-Password": "racedupdns"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/dns", data={"sailor_id": "S10"}, headers=pw)

    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/dns",
        data={"sailor_id": "S10"},
        headers=pw,
    )
    assert status == 400


def test_undo_dns(http_client):
    """DELETE /admin/races/{id}/finish/{sailor_id} should also undo a DNS."""
    eid = create_event(http_client, name="Race UndoDNS", admin_password="raceundodns")
    pw = {"X-Admin-Password": "raceundodns"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/dns", data={"sailor_id": "S10"}, headers=pw)

    status, body = http_client.delete(
        f"/api/event/{eid}/admin/races/{race['id']}/finish/S10",
        headers=pw,
    )
    assert status == 200
    assert len(body["finishers"]) == 0


# ── Undo finish ──────────────────────────────────────────────


def test_undo_finish(http_client):
    """DELETE /admin/races/{id}/finish/{sailor_id} should remove a result."""
    eid = create_event(http_client, name="Race Undo", admin_password="raceundo")
    pw = {"X-Admin-Password": "raceundo"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/start", data={"start_ts": start_ts}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/finish", data={"sailor_id": "S01", "finish_ts": start_ts + 300}, headers=pw)

    # Undo
    status, body = http_client.delete(
        f"/api/event/{eid}/admin/races/{race['id']}/finish/S01",
        headers=pw,
    )
    assert status == 200
    assert len(body["finishers"]) == 0


def test_undo_dnf(http_client):
    """Undo should also work for DNF entries."""
    eid = create_event(http_client, name="Race UndoDNF", admin_password="raceundodnf")
    pw = {"X-Admin-Password": "raceundodnf"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/dnf", data={"sailor_id": "S05"}, headers=pw)

    status, body = http_client.delete(
        f"/api/event/{eid}/admin/races/{race['id']}/finish/S05",
        headers=pw,
    )
    assert status == 200
    assert len(body["finishers"]) == 0


def test_undo_nonexistent_sailor(http_client):
    """Undo for a sailor not in the race should return 404."""
    eid = create_event(http_client, name="Race Undo404", admin_password="raceundo404")
    pw = {"X-Admin-Password": "raceundo404"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, _ = http_client.delete(
        f"/api/event/{eid}/admin/races/{race['id']}/finish/S99",
        headers=pw,
    )
    assert status == 404


def test_finish_after_undo(http_client):
    """After undoing, the sailor should be able to finish again."""
    eid = create_event(http_client, name="Race Re-Finish", admin_password="racerefinish")
    pw = {"X-Admin-Password": "racerefinish"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/start", data={"start_ts": start_ts}, headers=pw)

    # Finish, undo, re-finish
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/finish", data={"sailor_id": "S01", "finish_ts": start_ts + 300}, headers=pw)
    http_client.delete(f"/api/event/{eid}/admin/races/{race['id']}/finish/S01", headers=pw)

    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/finish",
        data={"sailor_id": "S01", "finish_ts": start_ts + 350},
        headers=pw,
    )
    assert status == 200
    assert len(body["finishers"]) == 1
    assert abs(body["finishers"][0]["finish_ts"] - (start_ts + 350)) < 0.01


# ── Delete race ──────────────────────────────────────────────


def test_delete_race(http_client):
    """DELETE /admin/races/{id} should remove the race."""
    eid = create_event(http_client, name="Race Delete", admin_password="racedelete")
    pw = {"X-Admin-Password": "racedelete"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, body = http_client.delete(
        f"/api/event/{eid}/admin/races/{race['id']}",
        headers=pw,
    )
    assert status == 200
    assert body["success"] is True

    # Verify gone
    status, body = http_client.get(f"/api/event/{eid}/races")
    assert len(body["races"]) == 0


def test_delete_nonexistent_race(http_client):
    """Deleting a nonexistent race should return 404."""
    eid = create_event(http_client, name="Race Del404", admin_password="racedel404")
    pw = {"X-Admin-Password": "racedel404"}
    status, _ = http_client.delete(
        f"/api/event/{eid}/admin/races/999",
        headers=pw,
    )
    assert status == 404


def test_delete_preserves_other_races(http_client):
    """Deleting one race should not affect others."""
    eid = create_event(http_client, name="Race DelOther", admin_password="racedelother")
    pw = {"X-Admin-Password": "racedelother"}
    _, r1 = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "Race A"}, headers=pw)
    _, r2 = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "Race B"}, headers=pw)

    http_client.delete(f"/api/event/{eid}/admin/races/{r1['id']}", headers=pw)

    status, body = http_client.get(f"/api/event/{eid}/races")
    assert len(body["races"]) == 1
    assert body["races"][0]["name"] == "Race B"


# ── Persistence ──────────────────────────────────────────────


def test_races_persist_across_reads(http_client):
    """Race data should persist across multiple reads (file-backed)."""
    eid = create_event(http_client, name="Race Persist", admin_password="racepersist")
    pw = {"X-Admin-Password": "racepersist"}

    # Create race, start it, add finisher
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/start", data={"start_ts": start_ts}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/finish", data={"sailor_id": "S01", "finish_ts": start_ts + 120}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/dnf", data={"sailor_id": "S02"}, headers=pw)

    # Re-read
    status, body = http_client.get(f"/api/event/{eid}/races")
    assert status == 200
    r = body["races"][0]
    assert r["name"] == "R1"
    assert abs(r["start_ts"] - start_ts) < 0.01
    assert len(r["finishers"]) == 2
    assert r["finishers"][0]["sailor_id"] == "S01"
    assert r["finishers"][0]["status"] == "finished"
    assert r["finishers"][1]["sailor_id"] == "S02"
    assert r["finishers"][1]["status"] == "dnf"


# ── Auth for all admin endpoints ─────────────────────────────


def test_start_requires_auth(http_client):
    """Start race should require admin auth."""
    eid = create_event(http_client, name="Race StartAuth", admin_password="racestartauth")
    pw = {"X-Admin-Password": "racestartauth"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/start",
        data={"start_ts": time.time()},
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.20.2.1"},
    )
    assert status == 401


def test_finish_requires_auth(http_client):
    """Record finish should require admin auth."""
    eid = create_event(http_client, name="Race FinAuth", admin_password="racefinauth")
    pw = {"X-Admin-Password": "racefinauth"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/finish",
        data={"sailor_id": "S01", "finish_ts": time.time()},
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.20.3.1"},
    )
    assert status == 401


def test_dnf_requires_auth(http_client):
    """DNF should require admin auth."""
    eid = create_event(http_client, name="Race DNFAuth", admin_password="racednfauth")
    pw = {"X-Admin-Password": "racednfauth"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/dnf",
        data={"sailor_id": "S01"},
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.20.4.1"},
    )
    assert status == 401


def test_undo_requires_auth(http_client):
    """Undo finish should require admin auth."""
    eid = create_event(http_client, name="Race UndoAuth", admin_password="raceundoauth")
    pw = {"X-Admin-Password": "raceundoauth"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/finish", data={"sailor_id": "S01", "finish_ts": time.time()}, headers=pw)

    status, _ = http_client.delete(
        f"/api/event/{eid}/admin/races/{race['id']}/finish/S01",
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.20.5.1"},
    )
    assert status == 401


# ── End race ─────────────────────────────────────────────────


def test_end_race(http_client):
    """POST /admin/races/{id}/end should set end time."""
    eid = create_event(http_client, name="Race End", admin_password="raceend")
    pw = {"X-Admin-Password": "raceend"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/start", data={"start_ts": start_ts}, headers=pw)

    end_ts = start_ts + 600
    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/end",
        data={"end_ts": end_ts},
        headers=pw,
    )
    assert status == 200
    assert abs(body["end_ts"] - end_ts) < 0.01


def test_reset_end_time(http_client):
    """Setting end_ts to null should clear the end time."""
    eid = create_event(http_client, name="Race ResetEnd", admin_password="raceresetend")
    pw = {"X-Admin-Password": "raceresetend"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/start", data={"start_ts": start_ts}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{race['id']}/end", data={"end_ts": start_ts + 600}, headers=pw)

    # Clear end time
    status, body = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/end",
        data={"end_ts": None},
        headers=pw,
    )
    assert status == 200
    assert body["end_ts"] is None


def test_end_requires_auth(http_client):
    """End race should require admin auth."""
    eid = create_event(http_client, name="Race EndAuth", admin_password="raceendauth")
    pw = {"X-Admin-Password": "raceendauth"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/{race['id']}/end",
        data={"end_ts": time.time()},
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.20.7.1"},
    )
    assert status == 401


def test_end_nonexistent_race(http_client):
    """Ending a nonexistent race should return 404."""
    eid = create_event(http_client, name="Race End404", admin_password="raceend404")
    pw = {"X-Admin-Password": "raceend404"}
    status, _ = http_client.post(
        f"/api/event/{eid}/admin/races/999/end",
        data={"end_ts": time.time()},
        headers=pw,
    )
    assert status == 404


def test_delete_requires_auth(http_client):
    """Delete race should require admin auth."""
    eid = create_event(http_client, name="Race DelAuth", admin_password="racedelauth")
    pw = {"X-Admin-Password": "racedelauth"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "R1"}, headers=pw)

    status, _ = http_client.delete(
        f"/api/event/{eid}/admin/races/{race['id']}",
        headers={"X-Admin-Password": "wrong", "X-Forwarded-For": "10.20.6.1"},
    )
    assert status == 401


# ── Log audit (destructive ops log full data for reconstruction) ─────


def _read_server_log(server):
    """Read the server log and return its contents."""
    server.process.stdout  # ensure flushed
    return server.log_file.read_text()


def test_delete_race_logs_full_data(http_client, server):
    """Deleting a race should log the full race JSON for reconstruction."""
    eid = create_event(http_client, name="Race LogDel", admin_password="racelogdel")
    pw = {"X-Admin-Password": "racelogdel"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "LoggedRace"}, headers=pw)
    rid = race['id']

    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/start", data={"start_ts": start_ts}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/finish",
                     data={"sailor_id": "S01", "finish_ts": start_ts + 100}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/dnf", data={"sailor_id": "S02"}, headers=pw)

    # Delete the race
    http_client.delete(f"/api/event/{eid}/admin/races/{rid}", headers=pw)
    time.sleep(0.2)

    log_text = _read_server_log(server)
    # Find the delete log line
    delete_lines = [l for l in log_text.splitlines() if f"Race {rid} deleted:" in l]
    assert delete_lines, "Expected a delete log line with full race data"

    # Extract JSON from the log line (everything after "deleted: ")
    json_str = delete_lines[-1].split("deleted: ", 1)[1]
    deleted_race = json.loads(json_str)
    assert deleted_race["name"] == "LoggedRace"
    assert len(deleted_race["finishers"]) == 2
    assert deleted_race["finishers"][0]["sailor_id"] == "S01"
    assert deleted_race["finishers"][0]["status"] == "finished"
    assert deleted_race["finishers"][1]["sailor_id"] == "S02"
    assert deleted_race["finishers"][1]["status"] == "dnf"


def test_reset_race_logs_cleared_results(http_client, server):
    """Resetting a race (start_ts=null) should log the cleared finishers."""
    eid = create_event(http_client, name="Race LogReset", admin_password="racelogreset")
    pw = {"X-Admin-Password": "racelogreset"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "ResetRace"}, headers=pw)
    rid = race['id']

    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/start", data={"start_ts": start_ts}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/finish",
                     data={"sailor_id": "S10", "finish_ts": start_ts + 200}, headers=pw)
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/dns", data={"sailor_id": "S11"}, headers=pw)

    # Reset (clear start)
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/start",
                     data={"start_ts": None}, headers=pw)
    time.sleep(0.2)

    log_text = _read_server_log(server)
    reset_lines = [l for l in log_text.splitlines() if f"Race {rid} reset" in l]
    assert reset_lines, "Expected a reset log line with cleared results"

    # Extract JSON array from the log line
    json_str = reset_lines[-1].split("clearing 2 results: ", 1)[1]
    cleared = json.loads(json_str)
    assert len(cleared) == 2
    sids = {f["sailor_id"] for f in cleared}
    assert sids == {"S10", "S11"}


def test_undo_finish_logs_removed_entry(http_client, server):
    """Undoing a finish should log the removed finisher entry."""
    eid = create_event(http_client, name="Race LogUndo", admin_password="racelogundo")
    pw = {"X-Admin-Password": "racelogundo"}
    _, race = http_client.post(f"/api/event/{eid}/admin/races", data={"name": "UndoRace"}, headers=pw)
    rid = race['id']

    start_ts = time.time()
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/start", data={"start_ts": start_ts}, headers=pw)
    finish_ts = start_ts + 150
    http_client.post(f"/api/event/{eid}/admin/races/{rid}/finish",
                     data={"sailor_id": "S20", "finish_ts": finish_ts}, headers=pw)

    # Undo
    http_client.delete(f"/api/event/{eid}/admin/races/{rid}/finish/S20", headers=pw)
    time.sleep(0.2)

    log_text = _read_server_log(server)
    undo_lines = [l for l in log_text.splitlines() if f"Race {rid}: undid result for S20:" in l]
    assert undo_lines, "Expected undo log line with removed entry data"

    json_str = undo_lines[-1].split("for S20: ", 1)[1]
    removed = json.loads(json_str)
    assert removed["sailor_id"] == "S20"
    assert removed["status"] == "finished"
    assert abs(removed["finish_ts"] - finish_ts) < 0.01
