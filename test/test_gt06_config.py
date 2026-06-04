"""Unit tests for load_gt06_config().

Regression: the config loader builds a whitelisted result dict, and the global
lag-remediation keys were not copied — so the production gt06.json
`lag_remediation_sec` was silently dropped and _resolve_setting() only ever saw
the per-device layer, leaving the feature disabled fleet-wide unless every IMEI
had its own config. (Found by codex review of 4c9d89b.)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from protocol_GT06 import load_gt06_config  # noqa: E402

_LAG_KEYS = (
    "lag_remediation_sec", "lag_drain_interval", "lag_restore_sec",
    "lag_remediation_cooldown_sec", "lag_remediation_max_retries",
    "lag_drain_max_sec",
)


def _load(tmp_path, cfg):
    p = tmp_path / "gt06.json"
    p.write_text(json.dumps(cfg))
    return load_gt06_config(p, log_func=lambda *_: None)


def test_global_lag_keys_propagate(tmp_path):
    """Global lag keys in gt06.json must survive into the loaded config so
    _resolve_setting() can read them without per-device overrides."""
    cfg = {
        "default_eid": 1,
        "lag_remediation_sec": 30, "lag_drain_interval": 2,
        "lag_restore_sec": 8, "lag_remediation_cooldown_sec": 60,
        "lag_remediation_max_retries": 3, "lag_drain_max_sec": 180,
        "devices": {},
    }
    loaded = _load(tmp_path, cfg)
    for k in _LAG_KEYS:
        assert loaded[k] == cfg[k], f"{k} dropped by load_gt06_config"


def test_lag_defaults_disabled_when_unset(tmp_path):
    """Unset -> remediation off (lag_remediation_sec=0)."""
    loaded = _load(tmp_path, {"default_eid": 1, "devices": {}})
    assert loaded["lag_remediation_sec"] == 0
    # The other knobs still get sensible defaults so an enabling per-device
    # override doesn't need to specify every key.
    assert loaded["lag_drain_interval"] == 2
    assert loaded["lag_restore_sec"] == 8
