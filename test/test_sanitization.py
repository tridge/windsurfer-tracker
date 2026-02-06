"""Tests for packet sanitization (pure unit tests, no server)."""

import sys
from pathlib import Path

import pytest

# Add server directory to path
SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from tracker_server import sanitize_tracker_packet


def test_html_tags_stripped():
    """HTML tags should be stripped from string fields."""
    packet = {
        "id": "<script>alert('xss')</script>S01",
        "role": "<b>sailor</b>",
        "ver": "<img src=x>v1",
        "sq": 1, "ts": 1000, "lat": 0, "lon": 0,
        "spd": 0, "hdg": 0, "ast": False, "bat": 50, "sig": 3,
    }
    result = sanitize_tracker_packet(packet)
    assert "<" not in result["id"]
    assert ">" not in result["id"]
    assert "<" not in result["role"]
    assert "<" not in result["ver"]


def test_lat_lon_clamped():
    """Lat/lon should be clamped to valid range."""
    packet = {
        "id": "S01", "sq": 1, "ts": 1000,
        "lat": 200.0, "lon": -300.0,
        "spd": 0, "hdg": 0, "ast": False, "bat": 50, "sig": 3,
        "role": "sailor", "ver": "test",
    }
    result = sanitize_tracker_packet(packet)
    assert result["lat"] == 90.0
    assert result["lon"] == -180.0


def test_battery_clamped():
    """Battery should be clamped to [-1, 100]."""
    packet = {
        "id": "S01", "sq": 1, "ts": 1000,
        "lat": 0, "lon": 0, "spd": 0, "hdg": 0,
        "ast": False, "bat": 150, "sig": 3,
        "role": "sailor", "ver": "test",
    }
    result = sanitize_tracker_packet(packet)
    assert result["bat"] == 100

    packet["bat"] = -5
    result = sanitize_tracker_packet(packet)
    assert result["bat"] == -1


def test_strings_truncated():
    """Strings should be truncated to max length."""
    long_id = "A" * 1000
    packet = {
        "id": long_id, "sq": 1, "ts": 1000,
        "lat": 0, "lon": 0, "spd": 0, "hdg": 0,
        "ast": False, "bat": 50, "sig": 3,
        "role": "sailor", "ver": "test",
    }
    result = sanitize_tracker_packet(packet)
    assert len(result["id"]) <= 32


def test_heading_clamped():
    """Heading should be clamped to [0, 360]."""
    packet = {
        "id": "S01", "sq": 1, "ts": 1000,
        "lat": 0, "lon": 0, "spd": 0, "hdg": 999,
        "ast": False, "bat": 50, "sig": 3,
        "role": "sailor", "ver": "test",
    }
    result = sanitize_tracker_packet(packet)
    assert result["hdg"] == 360
