"""Unit tests for the TERIID anti-spoofing core (HMAC prefix + login resolution).

TERIID = HMAC(master_key, IMEI)→9-digit prefix [1e8,998999999] + IMEI last6.
sailor_id stays G+last6 (no migration); did keeps the real IMEI. Trust anchor =
PROVISIONED units (gt06.json devices, provisioned=true), NOT device_state. Non-strict
preserves legacy routing; strict authenticates only valid TERIIDs.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_GT06 import GT06Listener  # noqa: E402

IMEI_A = "866557081378848"   # last6 378848
IMEI_B = "863874081226122"   # last6 226122


def _listener(strict=False, provisioned=(), key=b"master-key-test"):
    devices = {IMEI_A: {"eid": 4}, IMEI_B: {"eid": 2}}
    for im in provisioned:
        devices[im]["provisioned"] = True
    lst = GT06Listener(0, 1, "G", lambda *a, **k: None,
                       gt06_config={"default_eid": 1, "login_strict": strict,
                                    "devices": devices},
                       log_func=lambda *a, **k: None)
    # tests inject the key (no key file); re-assert strict only when a key is present
    # so the no-key guard (which forces non-strict at construction) can be tested too.
    lst._login_master_key = key
    if key is not None:
        lst.login_strict = strict
    return lst


# ---- HMAC / TERIID shape -----------------------------------------------------

def test_hmac_prefix_range_and_determinism():
    lst = _listener()
    p = lst._hmac_prefix(IMEI_A)
    assert 100000000 <= p <= 998999999 and not str(p).startswith("999")
    assert p == lst._hmac_prefix(IMEI_A) != lst._hmac_prefix(IMEI_B)


def test_hmac_prefix_none_without_key():
    lst = _listener(key=None)
    assert lst._hmac_prefix(IMEI_A) is None and lst._teriid_for(IMEI_A) is None


def test_teriid_shape():
    lst = _listener()
    t = lst._teriid_for(IMEI_A)
    assert len(t) == 15 and t.isdigit() and t[0] != "0"
    assert t.endswith("378848") and t[:9] == str(lst._hmac_prefix(IMEI_A))


# ---- resolution: trust anchor = provisioned set ------------------------------

def test_valid_teriid_authenticates_only_when_provisioned():
    # provisioned → valid TERIID authenticates
    lst = _listener(provisioned=[IMEI_A])
    assert lst._resolve_login(lst._teriid_for(IMEI_A)) == (True, IMEI_A, "auth")
    # NOT provisioned → its TERIID does not resolve (non-strict: routes as legacy)
    t = _listener()._teriid_for(IMEI_A)
    assert _listener(strict=False)._resolve_login(t) == (True, t, "legacy_raw")
    assert _listener(strict=True)._resolve_login(t) == (False, None, "recovery")


def test_raw_imei_legacy_vs_strict_onboard():
    # non-strict: raw IMEI routes (rollout). strict + assigned-but-unprovisioned: onboard
    assert _listener(strict=False)._resolve_login(IMEI_A) == (True, IMEI_A, "legacy_raw")
    assert _listener(strict=True)._resolve_login(IMEI_A) == (False, IMEI_A, "onboard")


def test_forged_prefix_spoof_only_in_strict_and_only_for_provisioned():
    p = _listener()._hmac_prefix(IMEI_A)
    forged = f"{p - 1 if p > 100000000 else p + 1}378848"   # known suffix, wrong prefix
    assert len(forged) == 15
    assert _listener(strict=False)._resolve_login(forged) == (True, forged, "legacy_raw")
    # strict + IMEI_A provisioned → suffix maps to a provisioned unit → spoof_alert
    assert _listener(strict=True, provisioned=[IMEI_A])._resolve_login(forged) == (False, IMEI_A, "spoof_alert")


def test_provisioned_unit_raw_imei_under_strict_is_spoof_not_onboard():
    # a provisioned unit's RAW imei under strict shares its (provisioned) suffix but
    # isn't its TERIID → spoof_alert, NEVER silently authenticated or onboardable.
    lst = _listener(strict=True, provisioned=[IMEI_B])
    assert lst._resolve_login(IMEI_B) == (False, IMEI_B, "spoof_alert")
    assert lst._resolve_login(lst._teriid_for(IMEI_B)) == (True, IMEI_B, "auth")


def test_unknown_15digit_legacy_vs_recovery():
    assert _listener(strict=False)._resolve_login("123456789012345") == (True, "123456789012345", "legacy_raw")
    assert _listener(strict=True)._resolve_login("123456789012345") == (False, None, "recovery")


def test_sim_and_garbled():
    a, imei, st = _listener()._resolve_login("99904000000123")    # 999-sim
    assert a is True and st == "sim"
    for strict in (False, True):
        assert _listener(strict=strict)._resolve_login("275808188e0d0d0") == (False, None, "recovery")


# ---- safety guards -----------------------------------------------------------

def test_strict_without_master_key_is_forced_nonstrict():
    # fleet-lockout footgun guard: strict requested + no key → forced non-strict
    lst = _listener(strict=True, key=None)
    assert lst.login_strict is False
    # and a raw IMEI still routes (legacy), not locked out
    assert lst._resolve_login(IMEI_A) == (True, IMEI_A, "legacy_raw")


def test_strict_with_key_file_survives_cold_start(tmp_path):
    # The init _apply_config runs before the config path is set; verify strict is
    # RESTORED once the sibling gt06_master_key file is loaded (cold-start bug fix).
    import json
    (tmp_path / "gt06_master_key").write_text("file-master-key\n")
    cfg = {"default_eid": 1, "login_strict": True,
           "devices": {IMEI_A: {"eid": 4, "provisioned": True}}}
    (tmp_path / "gt06.json").write_text(json.dumps(cfg))
    lst = GT06Listener(0, 1, "G", lambda *a, **k: None, gt06_config=cfg,
                       gt06_config_path=str(tmp_path / "gt06.json"),
                       log_func=lambda *a, **k: None)
    assert lst.login_strict is True                      # strict survived cold start
    assert lst._login_master_key == b"file-master-key"   # loaded from sibling file
    assert lst._resolve_login(lst._teriid_for(IMEI_A)) == (True, IMEI_A, "auth")
    assert lst._resolve_login(IMEI_A) == (False, IMEI_A, "spoof_alert")  # raw imei rejected


def test_publishes_gate():
    class C:
        pass
    c = C()
    # feature OFF (no master key): everything publishes (legacy / dev / pre-onboarding)
    off = _listener(key=None)
    c.auth_status = 'legacy_raw'
    assert off._publishes(c) is True
    # feature ON (master key): only registered (auth) + simulator publish
    on = _listener()
    for st, exp in [('auth', True), ('sim', True), ('legacy_raw', False),
                    ('onboard', False), ('recovery', False), ('spoof_alert', False)]:
        c.auth_status = st
        assert on._publishes(c) is exp, st


def test_redaction_masks_teriid_and_cxzt_id():
    from protocol_GT06 import _redact_teriid
    assert _redact_teriid("SZCS#TERIID=847291056378848#") == "SZCS#TERIID=***378848#"
    assert _redact_teriid("*ID:847291056378848*A:simbase") == "*ID:***378848*A:simbase"
    assert _redact_teriid("no secret") == "no secret"
