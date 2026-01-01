#!/usr/bin/env python3
"""
Test client - simulates multiple sailors, support boats, and spectators.
Sailors navigate between course waypoints with realistic tacking behavior.
Supports loading course from URL and land avoidance using coastline data.
"""

import socket
import json
import time
import argparse
import random
import math
import subprocess
import urllib.request
import os
import gzip
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict
from enum import Enum
from datetime import datetime


def get_git_hash() -> str:
    """Get short git hash for version tracking."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


GIT_HASH = get_git_hash()


class RaceState(Enum):
    """State machine for multi-race simulation"""
    PRE_RACE = 1      # Gathering before start
    RACING = 2        # Sailing the course
    POST_RACE = 3     # Gathering after finish


@dataclass
class SimulatedEntity:
    """Represents a tracked entity (sailor, support, spectator)"""
    id: str
    role: str
    lat: float
    lon: float
    hdg: float = 0.0
    spd: float = 0.0
    battery: int = 100
    signal: int = 4
    assist: bool = False
    seq: int = 0

    # Sailing state
    target_lat: float = 0.0
    target_lon: float = 0.0
    tack_timer: float = 0.0
    on_starboard: bool = True

    # Course navigation
    course_waypoints: List[Tuple[float, float]] = field(default_factory=list)
    current_waypoint_idx: int = 0
    current_lap: int = 0
    sailing_forward: bool = True  # True = toward finish, False = toward start

    # Movement parameters
    base_speed: float = 10.0
    speed_variance: float = 3.0

    # Wind gust simulation
    gust_factor: float = 1.0  # Multiplier for wind gusts (0.6 = lull, 1.4 = gust)
    gust_trend: float = 0.0   # Rate of change of gust factor

    # 1Hz mode (batched position updates)
    is_1hz: bool = False
    pos_buffer: List[Tuple[int, float, float, float]] = field(default_factory=list)  # [(ts, lat, lon, spd), ...]
    heart_rate: int = 0  # Only used in 1Hz mode

    # Race state tracking (for multi-race simulation)
    race_state: RaceState = RaceState.PRE_RACE
    mark_order: List[int] = field(default_factory=list)  # Sequence of mark indices to round
    mark_order_idx: int = 0  # Current position in mark_order
    has_finished: bool = False  # True when sailor has crossed finish line
    race_waypoints: List[Tuple[float, float]] = field(default_factory=list)  # Waypoints with roundings
    race_wp_idx: int = 0  # Current index in race_waypoints

    def __post_init__(self):
        self.target_lat = self.lat
        self.target_lon = self.lon
        self.gust_factor = random.uniform(0.9, 1.1)  # Start with slight variation
        self.gust_trend = random.uniform(-0.05, 0.05)
        if self.is_1hz:
            self.heart_rate = random.randint(60, 90)  # Initial heart rate


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in meters"""
    R = 6371000  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))


def bearing_to(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate bearing from point 1 to point 2 in degrees"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)

    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def move_point(lat: float, lon: float, bearing: float, distance_m: float) -> Tuple[float, float]:
    """Move a point by distance in meters along bearing"""
    R = 6371000
    d = distance_m / R

    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    bearing_rad = math.radians(bearing)

    lat2 = math.asin(math.sin(lat1) * math.cos(d) +
                     math.cos(lat1) * math.sin(d) * math.cos(bearing_rad))
    lon2 = lon1 + math.atan2(math.sin(bearing_rad) * math.sin(d) * math.cos(lat1),
                             math.cos(d) - math.sin(lat1) * math.sin(lat2))

    return math.degrees(lat2), math.degrees(lon2)


def point_in_polygon(lat: float, lon: float, polygon: List[Tuple[float, float]]) -> bool:
    """Ray casting algorithm - returns True if point is inside polygon.

    polygon is a list of (lat, lon) tuples forming a closed polygon.
    """
    n = len(polygon)
    if n < 3:
        return False

    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]

        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i

    return inside


class CoastlineData:
    """Manages coastline polygons for land detection."""

    def __init__(self, land_polygons: List[List[Tuple[float, float]]],
                 bounds: Tuple[float, float, float, float]):
        self.land_polygons = land_polygons
        self.bounds = bounds  # (min_lat, max_lat, min_lon, max_lon)

    def is_on_land(self, lat: float, lon: float) -> bool:
        """Check if a point is on land."""
        # Quick bounds check
        if not (self.bounds[0] <= lat <= self.bounds[1] and
                self.bounds[2] <= lon <= self.bounds[3]):
            return False  # Outside data area, assume water

        for polygon in self.land_polygons:
            if point_in_polygon(lat, lon, polygon):
                return True
        return False


def load_coastline(path: str) -> Optional[CoastlineData]:
    """Load coastline data from GeoJSON file.

    Expected format:
    {
        "bounds": {"min_lat": x, "max_lat": x, "min_lon": x, "max_lon": x},
        "land_polygons": [[[lat, lon], [lat, lon], ...], ...]
    }

    Or standard GeoJSON with Polygon/MultiPolygon features.
    """
    try:
        with open(path, 'r') as f:
            data = json.load(f)

        # Check for our simple format first
        if 'land_polygons' in data and 'bounds' in data:
            bounds = (data['bounds']['min_lat'], data['bounds']['max_lat'],
                      data['bounds']['min_lon'], data['bounds']['max_lon'])
            polygons = [[(p[0], p[1]) for p in poly] for poly in data['land_polygons']]
            return CoastlineData(polygons, bounds)

        # Try standard GeoJSON format
        polygons = []
        min_lat, max_lat = 90, -90
        min_lon, max_lon = 180, -180

        features = data.get('features', [data] if data.get('type') == 'Feature' else [])
        for feature in features:
            geom = feature.get('geometry', feature)
            geom_type = geom.get('type', '')

            if geom_type == 'Polygon':
                # First ring is exterior
                coords = geom['coordinates'][0]
                # GeoJSON is [lon, lat], we need (lat, lon)
                polygon = [(c[1], c[0]) for c in coords]
                polygons.append(polygon)
                for lat, lon in polygon:
                    min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
                    min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)

            elif geom_type == 'MultiPolygon':
                for poly_coords in geom['coordinates']:
                    coords = poly_coords[0]  # First ring
                    polygon = [(c[1], c[0]) for c in coords]
                    polygons.append(polygon)
                    for lat, lon in polygon:
                        min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
                        min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)

        if polygons:
            bounds = (min_lat, max_lat, min_lon, max_lon)
            return CoastlineData(polygons, bounds)

        print(f"Warning: No valid polygons found in {path}")
        return None

    except Exception as e:
        print(f"Warning: Could not load coastline from {path}: {e}")
        return None


@dataclass
class CourseData:
    """Course data including waypoints and mark colors"""
    waypoints: List[Tuple[float, float]]  # [(lat, lon), ...]
    mark_colors: Dict[int, str]  # {mark_index: color_hex, ...}


def load_course(source: str) -> Optional[CourseData]:
    """Load course data from URL or local file.

    Returns CourseData with waypoints list and mark colors dict.
    Waypoints are in order: [start, mark1, mark2, ..., finish]
    """
    try:
        # Check if source is a local file
        if os.path.exists(source):
            print(f"Loading course from file: {source}")
            with open(source, 'r') as f:
                data = json.load(f)
        else:
            # Assume it's a URL
            print(f"Loading course from URL: {source}")
            req = urllib.request.Request(source, headers={'User-Agent': 'WindsurferTracker/1.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))

        waypoints = []
        mark_colors = {}

        # Add start point (index 0)
        if 'start' in data:
            waypoints.append((data['start']['lat'], data['start']['lon']))

        # Add marks (indices 1, 2, 3, ...)
        if 'marks' in data:
            for i, mark in enumerate(data['marks']):
                waypoints.append((mark['lat'], mark['lon']))
                if 'color' in mark:
                    mark_colors[i + 1] = mark['color']  # +1 because start is index 0

        # Add finish point (if different from start)
        if 'finish' in data:
            finish = (data['finish']['lat'], data['finish']['lon'])
            # Only add if different from last mark
            if not waypoints or haversine_distance(waypoints[-1][0], waypoints[-1][1],
                                                    finish[0], finish[1]) > 10:
                waypoints.append(finish)

        if len(waypoints) < 2:
            print(f"Warning: Course has fewer than 2 waypoints")
            return None

        print(f"Loaded course with {len(waypoints)} waypoints")
        if mark_colors:
            print(f"  Mark colors: {mark_colors}")
        return CourseData(waypoints=waypoints, mark_colors=mark_colors)

    except Exception as e:
        print(f"Warning: Could not load course from {source}: {e}")
        return None


def get_gathering_area(course_waypoints: List[Tuple[float, float]]) -> Tuple[Tuple[float, float], float]:
    """Calculate 100m square behind start line (downwind of start).

    Returns (center_lat_lon, downwind_bearing)
    """
    start = course_waypoints[0]  # Start/mark 1
    mark2 = course_waypoints[1]  # Second mark (first leg is upwind to this mark)

    # Bearing from start to mark2 is upwind direction
    upwind_bearing = bearing_to(start[0], start[1], mark2[0], mark2[1])
    downwind_bearing = (upwind_bearing + 180) % 360

    # Center of gathering area is 75m downwind of start
    center = move_point(start[0], start[1], downwind_bearing, 75)

    return center, downwind_bearing


def parse_mark_order(mark_order_str: str, num_waypoints: int) -> List[int]:
    """Parse mark order string into list of waypoint indices.

    Args:
        mark_order_str: Comma-separated mark indices, e.g., "1,2,3,1,2,1"
        num_waypoints: Total number of waypoints in the course (including start)

    Returns:
        List of waypoint indices (1=first mark after start, 2=second mark, etc.)
    """
    max_idx = num_waypoints - 1  # Highest valid index (0 is start)

    if not mark_order_str:
        # Default: all marks in sequence (1, 2, 3, ... max_idx)
        return list(range(1, max_idx + 1))

    try:
        # Parse comma-separated values
        indices = [int(x.strip()) for x in mark_order_str.split(',')]
        # Filter out invalid indices with warning
        valid_indices = []
        for idx in indices:
            if idx < 1 or idx > max_idx:
                print(f"Warning: Mark index {idx} out of range (1-{max_idx}), skipping")
            else:
                valid_indices.append(idx)
        if not valid_indices:
            print(f"Warning: No valid marks in order, using default sequence")
            return list(range(1, max_idx + 1))
        return valid_indices
    except ValueError as e:
        print(f"Warning: Invalid mark order '{mark_order_str}': {e}")
        return list(range(1, max_idx + 1))


def is_port_rounding(color: Optional[str]) -> bool:
    """Check if mark color indicates port rounding (red = port, green = starboard)."""
    if not color:
        return False  # Default to starboard rounding
    color_lower = color.lower()
    # Red colors indicate port rounding
    # Check for common red hex patterns: #ff0000, #f00, #ef4444, etc.
    if 'red' in color_lower:
        return True
    if color_lower.startswith('#'):
        # Parse hex color - check if it's reddish (high R, low G/B)
        hex_color = color_lower[1:]
        if len(hex_color) == 3:
            r, g, b = int(hex_color[0], 16) * 17, int(hex_color[1], 16) * 17, int(hex_color[2], 16) * 17
        elif len(hex_color) == 6:
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        else:
            return False
        # Red if R is high (>180) and R is significantly higher than G and B
        return r > 180 and r > g * 1.5 and r > b * 1.5
    return False


def build_rounding_waypoints(prev_pos: Tuple[float, float], mark_pos: Tuple[float, float],
                              next_pos: Tuple[float, float], port_rounding: bool,
                              offset_dist: float = 30) -> List[Tuple[float, float]]:
    """Build waypoints to properly round a mark.

    Args:
        prev_pos: Position coming from (lat, lon)
        mark_pos: Mark position (lat, lon)
        next_pos: Position going to after mark (lat, lon)
        port_rounding: True if mark should be kept on port (left) side
        offset_dist: Distance in meters to offset from mark

    Returns:
        List of waypoints to navigate through for proper rounding
    """
    # Calculate approach and exit bearings
    approach_bearing = bearing_to(prev_pos[0], prev_pos[1], mark_pos[0], mark_pos[1])
    exit_bearing = bearing_to(mark_pos[0], mark_pos[1], next_pos[0], next_pos[1])

    # For port rounding (mark on left): pass on starboard (right) side
    # For starboard rounding (mark on right): pass on port (left) side
    if port_rounding:
        # Offset to starboard (right) - perpendicular clockwise
        approach_offset_bearing = (approach_bearing + 90) % 360
        exit_offset_bearing = (exit_bearing + 90) % 360
    else:
        # Offset to port (left) - perpendicular counter-clockwise
        approach_offset_bearing = (approach_bearing - 90) % 360
        exit_offset_bearing = (exit_bearing - 90) % 360

    # Create approach and exit waypoints offset from the mark
    approach_wp = move_point(mark_pos[0], mark_pos[1], approach_offset_bearing, offset_dist)
    exit_wp = move_point(mark_pos[0], mark_pos[1], exit_offset_bearing, offset_dist)

    return [approach_wp, exit_wp]


def build_race_waypoints(start_pos: Tuple[float, float], course_waypoints: List[Tuple[float, float]],
                          mark_order: List[int], mark_colors: Dict[int, str]) -> List[Tuple[float, float]]:
    """Build full list of waypoints for a race including mark roundings.

    Args:
        start_pos: Starting position (gathering area center)
        course_waypoints: All course waypoints [start, mark1, mark2, ..., finish]
        mark_order: Sequence of mark indices to round
        mark_colors: Dict of mark_index -> color for rounding direction

    Returns:
        List of waypoints to navigate through
    """
    if not mark_order or not course_waypoints:
        return []

    waypoints = []
    prev_pos = start_pos

    # Determine finish position (last waypoint or start position)
    finish_pos = course_waypoints[-1] if len(course_waypoints) > 1 else start_pos

    for i, mark_idx in enumerate(mark_order):
        mark_pos = course_waypoints[mark_idx]

        # Determine next position (next mark or finish)
        if i + 1 < len(mark_order):
            next_mark_idx = mark_order[i + 1]
            next_pos = course_waypoints[next_mark_idx]
        else:
            # Last mark - head to finish line
            next_pos = finish_pos

        # Get rounding direction from mark color
        port_rounding = is_port_rounding(mark_colors.get(mark_idx))

        # Build rounding waypoints
        rounding_wps = build_rounding_waypoints(prev_pos, mark_pos, next_pos, port_rounding)
        waypoints.extend(rounding_wps)

        prev_pos = mark_pos

    # Add finish line as final waypoint
    waypoints.append(finish_pos)

    return waypoints


def calculate_wind_from_course(course_waypoints: List[Tuple[float, float]]) -> float:
    """Calculate wind direction so first leg is into the wind.

    Wind direction is the compass direction wind blows FROM.
    If first leg (start → mark1) is upwind, wind blows FROM mark1's direction.
    """
    start = course_waypoints[0]
    mark1 = course_waypoints[1]
    # Wind direction = bearing FROM start TO mark1 (direction wind comes from)
    return bearing_to(start[0], start[1], mark1[0], mark1[1])


def write_log_entry(f, entity: 'SimulatedEntity', ts: int):
    """Write a single position entry to log file in server's JSONL format."""
    entry = {
        "recv_ts": ts + 0.1,
        "id": entity.id,
        "ts": ts,
        "lat": round(entity.lat, 6),
        "lon": round(entity.lon, 6),
        "spd": round(entity.spd, 2),
        "hdg": int(entity.hdg) % 360,
        "ast": entity.assist,
        "bat": entity.battery,
        "sig": entity.signal,
        "role": entity.role,
        "ver": GIT_HASH
    }
    if entity.heart_rate > 0:
        entry["hr"] = entity.heart_rate
    f.write(json.dumps(entry) + "\n")


def write_log_entry_1hz(f, entity: 'SimulatedEntity', pos_buffer: List[Tuple[int, float, float, float]]):
    """Write a 1Hz batch entry with pos array to log file.

    pos_buffer is list of (ts, lat, lon, spd) tuples collected over ~10 seconds.
    """
    if not pos_buffer:
        return

    last_ts = pos_buffer[-1][0]
    entry = {
        "recv_ts": last_ts + 0.1,
        "id": entity.id,
        "ts": last_ts,
        "pos": [[ts, round(lat, 6), round(lon, 6), round(spd, 1)] for ts, lat, lon, spd in pos_buffer],
        "spd": round(entity.spd, 2),
        "hdg": int(entity.hdg) % 360,
        "ast": entity.assist,
        "bat": entity.battery,
        "sig": entity.signal,
        "role": entity.role,
        "ver": GIT_HASH,
        "hac": 5.0  # Simulated horizontal accuracy
    }
    if entity.heart_rate > 0:
        entry["hr"] = entity.heart_rate
    f.write(json.dumps(entry) + "\n")


class SailingSimulator:
    """Simulates realistic sailing movement with tacking"""

    TACK_ANGLE = 45      # Angle to wind when close-hauled

    def __init__(self, start_loc: Tuple[float, float], end_loc: Tuple[float, float],
                 wind_direction: float = 45, coastline: Optional[CoastlineData] = None,
                 num_laps: int = 1):
        self.start_lat, self.start_lon = start_loc
        self.end_lat, self.end_lon = end_loc
        self.wind_direction = wind_direction
        self.coastline = coastline
        self.num_laps = num_laps

    def _check_and_avoid_land(self, entity: SimulatedEntity, new_lat: float, new_lon: float,
                               distance_m: float) -> Tuple[float, float, float]:
        """Check if new position is on land and find alternative heading if needed.

        Returns (lat, lon, heading) - either the original or an adjusted position.
        """
        if not self.coastline or not self.coastline.is_on_land(new_lat, new_lon):
            return new_lat, new_lon, entity.hdg

        # Try alternative headings to avoid land
        for angle_adjust in [30, -30, 60, -60, 90, -90, 120, -120, 150, -150, 180]:
            alt_hdg = (entity.hdg + angle_adjust) % 360
            alt_lat, alt_lon = move_point(entity.lat, entity.lon, alt_hdg, distance_m)
            if not self.coastline.is_on_land(alt_lat, alt_lon):
                return alt_lat, alt_lon, alt_hdg

        # All directions blocked - stay in place
        return entity.lat, entity.lon, entity.hdg

    def update_sailor(self, entity: SimulatedEntity, dt: float):
        """Update sailor position with tacking behavior and course navigation"""

        # Determine target based on course waypoints or simple start/end
        if entity.course_waypoints:
            # Navigate between course waypoints, sailing back and forth
            target_lat, target_lon = entity.course_waypoints[entity.current_waypoint_idx]
            distance_to_waypoint = haversine_distance(entity.lat, entity.lon, target_lat, target_lon)

            # Check if reached waypoint (within 30m)
            if distance_to_waypoint < 30:
                if entity.sailing_forward:
                    # Moving toward finish
                    if entity.current_waypoint_idx >= len(entity.course_waypoints) - 1:
                        # Reached the end - reverse direction
                        entity.sailing_forward = False
                        entity.current_waypoint_idx = len(entity.course_waypoints) - 2
                        entity.current_lap += 1
                    else:
                        entity.current_waypoint_idx += 1
                else:
                    # Moving toward start
                    if entity.current_waypoint_idx <= 0:
                        # Reached the start - reverse direction
                        entity.sailing_forward = True
                        entity.current_waypoint_idx = 1
                        entity.current_lap += 1
                    else:
                        entity.current_waypoint_idx -= 1

                # Update target
                target_lat, target_lon = entity.course_waypoints[entity.current_waypoint_idx]

            entity.target_lat, entity.target_lon = target_lat, target_lon
        else:
            # Simple back-and-forth between start and end
            target_lat, target_lon = entity.target_lat, entity.target_lon
            distance = haversine_distance(entity.lat, entity.lon, target_lat, target_lon)

            # Check if reached target, swap start/end
            if distance < 50:  # Within 50m
                if abs(entity.target_lat - self.end_lat) < 0.0001:
                    entity.target_lat, entity.target_lon = self.start_lat, self.start_lon
                else:
                    entity.target_lat, entity.target_lon = self.end_lat, self.end_lon

        # Calculate bearing to target
        target_bearing = bearing_to(entity.lat, entity.lon, entity.target_lat, entity.target_lon)

        # Determine if we need to tack (sailing into wind)
        wind_angle = (target_bearing - self.wind_direction + 360) % 360

        if wind_angle < self.TACK_ANGLE or wind_angle > (360 - self.TACK_ANGLE):
            # Need to tack - can't sail directly into wind
            entity.tack_timer -= dt

            if entity.tack_timer <= 0:
                # Time to tack
                entity.on_starboard = not entity.on_starboard
                entity.tack_timer = random.uniform(30, 60)  # Tack every 30-60 seconds

            # Sail at angle to wind
            if entity.on_starboard:
                entity.hdg = (self.wind_direction + self.TACK_ANGLE) % 360
            else:
                entity.hdg = (self.wind_direction - self.TACK_ANGLE + 360) % 360

            # Slower when beating
            base_spd = entity.base_speed * 0.7

        elif 60 < wind_angle < 120 or 240 < wind_angle < 300:
            # Reaching - fastest point of sail
            entity.hdg = target_bearing + random.uniform(-10, 10)
            base_spd = entity.base_speed * 1.2
            entity.tack_timer = 0

        else:
            # Running or broad reach
            entity.hdg = target_bearing + random.uniform(-15, 15)
            base_spd = entity.base_speed * 0.9
            entity.tack_timer = 0

        # Ensure heading is in valid range
        entity.hdg = (entity.hdg + 360) % 360

        # Update gust factor - simulates wind variability (smoother changes)
        entity.gust_trend += random.uniform(-0.01, 0.01) * dt
        entity.gust_trend = max(-0.08, min(0.08, entity.gust_trend))  # Limit trend rate
        entity.gust_factor += entity.gust_trend * dt
        entity.gust_factor = max(0.7, min(1.4, entity.gust_factor))  # Keep within 70%-140%

        # Occasional sudden gusts or lulls (less frequent)
        if random.random() < 0.005 * dt:  # ~0.5% chance per second
            entity.gust_factor += random.choice([-0.15, 0.2])  # Sudden lull or gust
            entity.gust_factor = max(0.6, min(1.5, entity.gust_factor))

        # Calculate target speed from wind conditions
        target_spd = base_spd * entity.gust_factor

        # Smooth speed transitions using exponential moving average
        # Speed changes gradually toward target (time constant ~3 seconds)
        alpha = min(1.0, dt / 3.0)  # Smoothing factor
        entity.spd = entity.spd * (1 - alpha) + target_spd * alpha

        # Ensure speed is positive
        entity.spd = max(0.5, entity.spd)

        # Calculate new position
        distance_m = entity.spd * 0.514444 * dt  # knots to m/s, then * time
        new_lat, new_lon = move_point(entity.lat, entity.lon, entity.hdg, distance_m)

        # Check for land and adjust if needed
        entity.lat, entity.lon, entity.hdg = self._check_and_avoid_land(
            entity, new_lat, new_lon, distance_m)

    def update_support(self, entity: SimulatedEntity, dt: float, sailors: List[SimulatedEntity]):
        """Update support boat - patrols near sailors"""
        if not sailors:
            return

        # Find center of sailors
        center_lat = sum(s.lat for s in sailors) / len(sailors)
        center_lon = sum(s.lon for s in sailors) / len(sailors)

        # Move toward center with some randomness
        target_bearing = bearing_to(entity.lat, entity.lon, center_lat, center_lon)
        distance = haversine_distance(entity.lat, entity.lon, center_lat, center_lon)

        if distance > 200:  # More than 200m from center
            entity.hdg = target_bearing + random.uniform(-20, 20)
            entity.spd = 8 + random.uniform(-2, 2)
        else:
            # Patrol in circles
            entity.hdg = (entity.hdg + random.uniform(2, 5)) % 360
            entity.spd = 3 + random.uniform(-1, 1)

        entity.spd = max(0, entity.spd)
        distance_m = entity.spd * 0.514444 * dt
        new_lat, new_lon = move_point(entity.lat, entity.lon, entity.hdg, distance_m)

        # Check for land
        entity.lat, entity.lon, entity.hdg = self._check_and_avoid_land(
            entity, new_lat, new_lon, distance_m)

    def update_spectator(self, entity: SimulatedEntity, dt: float):
        """Update spectator - mostly stationary with drift"""
        # Slow random drift
        entity.hdg = (entity.hdg + random.uniform(-5, 5)) % 360
        entity.spd = random.uniform(0, 0.5)

        distance_m = entity.spd * 0.514444 * dt
        new_lat, new_lon = move_point(entity.lat, entity.lon, entity.hdg, distance_m)

        # Check for land
        entity.lat, entity.lon, entity.hdg = self._check_and_avoid_land(
            entity, new_lat, new_lon, distance_m)


def create_entities(num_sailors: int, num_support: int, num_spectators: int,
                    start_loc: Tuple[float, float], end_loc: Tuple[float, float],
                    course_waypoints: Optional[List[Tuple[float, float]]] = None,
                    avg_speed: float = 12.0) -> List[SimulatedEntity]:
    """Create all simulated entities spread along the course.

    avg_speed: Average sailor speed in knots. Individual speeds are normally
               distributed with std dev of 20% of avg_speed.
    """
    entities = []

    if course_waypoints and len(course_waypoints) >= 2:
        # Calculate course path length (open path, not closed loop)
        # Segments go from waypoint[i] to waypoint[i+1] for i in 0..n-2
        num_segments = len(course_waypoints) - 1
        segment_lengths = []
        total_length = 0
        for i in range(num_segments):
            dist = haversine_distance(course_waypoints[i][0], course_waypoints[i][1],
                                      course_waypoints[i+1][0], course_waypoints[i+1][1])
            segment_lengths.append(dist)
            total_length += dist

        def position_along_course(progress: float) -> Tuple[float, float, int]:
            """Get lat/lon and next waypoint index for a position along the course.

            progress: 0.0 = start, 1.0 = finish
            Returns: (lat, lon, next_waypoint_idx)
            """
            target_distance = progress * total_length

            # Find which segment this falls on
            cumulative = 0
            segment_idx = 0
            for j, seg_len in enumerate(segment_lengths):
                if cumulative + seg_len >= target_distance:
                    segment_idx = j
                    break
                cumulative += seg_len
            else:
                # Past the end, use last segment
                segment_idx = num_segments - 1
                cumulative = total_length - segment_lengths[-1]

            # Interpolate position within segment
            seg_len = segment_lengths[segment_idx]
            segment_progress = (target_distance - cumulative) / max(0.1, seg_len)
            segment_progress = max(0, min(1, segment_progress))

            wp1 = course_waypoints[segment_idx]
            wp2 = course_waypoints[segment_idx + 1]

            lat = wp1[0] + (wp2[0] - wp1[0]) * segment_progress
            lon = wp1[1] + (wp2[1] - wp1[1]) * segment_progress

            # Next waypoint is the end of current segment
            next_idx = segment_idx + 1

            return lat, lon, next_idx

        # Spread sailors evenly along the entire course path
        for i in range(num_sailors):
            # Position along course (0.0 to 1.0) with small random offset
            progress = i / max(1, num_sailors)
            progress += random.uniform(0, 0.8 / max(1, num_sailors))
            progress = min(0.95, progress)  # Don't start right at finish

            lat, lon, next_idx = position_along_course(progress)

            # Add small random offset perpendicular to course
            lat += random.uniform(-0.0003, 0.0003)
            lon += random.uniform(-0.0003, 0.0003)

            target_lat, target_lon = course_waypoints[next_idx]

            # Odd-numbered sailors use 1Hz mode (batched position updates)
            use_1hz = (i % 2) == 0  # Test01, Test03, Test05, ... (0-indexed: 0, 2, 4, ...)

            entity = SimulatedEntity(
                id=f"Test{i+1:02d}",
                role="sailor",
                lat=lat,
                lon=lon,
                target_lat=target_lat,
                target_lon=target_lon,
                course_waypoints=list(course_waypoints),
                current_waypoint_idx=next_idx,
                base_speed=max(4, random.gauss(avg_speed, avg_speed * 0.2)),  # Normal dist, min 4 kts
                battery=random.randint(70, 100),
                signal=random.randint(2, 4),
                on_starboard=random.choice([True, False]),
                tack_timer=random.uniform(30, 60),
                is_1hz=use_1hz
            )
            entities.append(entity)

        # Support boats spread along course
        for i in range(num_support):
            progress = (i + 0.5) / max(1, num_support)
            lat, lon, _ = position_along_course(progress)
            lat += random.uniform(-0.001, 0.001)
            lon += random.uniform(-0.001, 0.001)

            entity = SimulatedEntity(
                id=f"Rescue{i+1:02d}",
                role="support",
                lat=lat,
                lon=lon,
                battery=random.randint(80, 100),
                signal=random.randint(3, 4)
            )
            entities.append(entity)

        # Spectators near the start/finish area
        if course_waypoints:
            spec_lat, spec_lon = course_waypoints[0]
            for i in range(num_spectators):
                lat = spec_lat + random.uniform(-0.002, 0.002)
                lon = spec_lon + random.uniform(0.002, 0.005)  # Offset to east
                entity = SimulatedEntity(
                    id=f"V{i+1:02d}",
                    role="spectator",
                    lat=lat,
                    lon=lon,
                    battery=random.randint(50, 100),
                    signal=random.randint(1, 4)
                )
                entities.append(entity)
    else:
        # Original behavior - spread along start to end line
        for i in range(num_sailors):
            progress = i / max(1, num_sailors - 1) if num_sailors > 1 else 0.5
            base_lat = start_loc[0] + (end_loc[0] - start_loc[0]) * progress
            base_lon = start_loc[1] + (end_loc[1] - start_loc[1]) * progress
            lat = base_lat + random.uniform(-0.002, 0.002)
            lon = base_lon + random.uniform(-0.002, 0.002)
            if progress < 0.5:
                target_lat, target_lon = end_loc[0], end_loc[1]
            else:
                target_lat, target_lon = start_loc[0], start_loc[1]
            # Odd-numbered sailors use 1Hz mode (batched position updates)
            use_1hz = (i % 2) == 0  # Test01, Test03, Test05, ...

            entity = SimulatedEntity(
                id=f"Test{i+1:02d}",
                role="sailor",
                lat=lat,
                lon=lon,
                target_lat=target_lat,
                target_lon=target_lon,
                base_speed=max(4, random.gauss(avg_speed, avg_speed * 0.2)),  # Normal dist, min 4 kts
                battery=random.randint(70, 100),
                signal=random.randint(2, 4),
                on_starboard=random.choice([True, False]),
                tack_timer=random.uniform(30, 60),
                is_1hz=use_1hz
            )
            entities.append(entity)

        for i in range(num_support):
            progress = (i + 0.5) / num_support if num_support > 0 else 0.5
            base_lat = start_loc[0] + (end_loc[0] - start_loc[0]) * progress
            base_lon = start_loc[1] + (end_loc[1] - start_loc[1]) * progress
            lat = base_lat + random.uniform(-0.001, 0.001)
            lon = base_lon + random.uniform(-0.001, 0.001)
            entity = SimulatedEntity(
                id=f"Rescue{i+1:02d}",
                role="support",
                lat=lat,
                lon=lon,
                battery=random.randint(80, 100),
                signal=random.randint(3, 4)
            )
            entities.append(entity)

        mid_lat = (start_loc[0] + end_loc[0]) / 2
        mid_lon = (start_loc[1] + end_loc[1]) / 2
        for i in range(num_spectators):
            progress = (i + 0.5) / num_spectators if num_spectators > 0 else 0.5
            base_lat = start_loc[0] + (end_loc[0] - start_loc[0]) * progress
            base_lon = start_loc[1] + (end_loc[1] - start_loc[1]) * progress
            lat = base_lat + random.uniform(-0.001, 0.001)
            lon = base_lon + random.uniform(0.002, 0.005)
            entity = SimulatedEntity(
                id=f"V{i+1:02d}",
                role="spectator",
                lat=lat,
                lon=lon,
                battery=random.randint(50, 100),
                signal=random.randint(1, 4)
            )
            entities.append(entity)

    return entities


def send_packet(sock: socket.socket, host: str, port: int, entity: SimulatedEntity,
                password: str = "", eid: int = 1) -> bool:
    """Send position packet and wait for ACK"""
    entity.seq += 1

    packet = {
        "id": entity.id,
        "eid": eid,
        "sq": entity.seq,
        "ts": int(time.time()),
        "lat": round(entity.lat, 6),
        "lon": round(entity.lon, 6),
        "hac": 0.5,
        "spd": round(entity.spd, 2),
        "hdg": int(entity.hdg) % 360,
        "ast": entity.assist,
        "bat": entity.battery,
        "sig": entity.signal,
        "role": entity.role,
        "ver": GIT_HASH
    }

    if password:
        packet["pwd"] = password

    data = json.dumps(packet).encode("utf-8")
    sock.sendto(data, (host, port))

    try:
        ack_data, _ = sock.recvfrom(256)
        # Check if response contains an error
        try:
            ack = json.loads(ack_data.decode('utf-8'))
            if 'error' in ack:
                return False  # Got error response
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return True
    except socket.timeout:
        return False


def send_packet_1hz(sock: socket.socket, host: str, port: int, entity: SimulatedEntity,
                    password: str = "", eid: int = 1) -> bool:
    """Send 1Hz batch position packet with pos array and wait for ACK"""
    entity.seq += 1

    # pos array format: [[ts, lat, lon, spd], ...]
    pos_array = [[ts, round(lat, 6), round(lon, 6), round(spd, 1)] for ts, lat, lon, spd in entity.pos_buffer]

    packet = {
        "id": entity.id,
        "eid": eid,
        "sq": entity.seq,
        "ts": int(time.time()),  # Current timestamp (for sorting)
        "pos": pos_array,        # Array of [ts, lat, lon, spd] positions
        "hac": 0.5,
        "spd": round(entity.spd, 2),
        "hdg": int(entity.hdg) % 360,
        "ast": entity.assist,
        "bat": entity.battery,
        "sig": entity.signal,
        "role": entity.role,
        "ver": GIT_HASH,
        "hr": entity.heart_rate   # Heart rate included in 1Hz packets
    }

    if password:
        packet["pwd"] = password

    data = json.dumps(packet).encode("utf-8")
    sock.sendto(data, (host, port))

    # Clear the buffer after sending
    entity.pos_buffer.clear()

    try:
        ack_data, _ = sock.recvfrom(256)
        # Check if response contains an error
        try:
            ack = json.loads(ack_data.decode('utf-8'))
            if 'error' in ack:
                return False  # Got error response
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return True
    except socket.timeout:
        return False


def update_gathering_sailor(entity: SimulatedEntity, gathering_center: Tuple[float, float],
                            downwind_bearing: float, dt: float):
    """Update sailor position during pre-race or post-race gathering."""
    center_lat, center_lon = gathering_center

    # Check distance from center
    dist_to_center = haversine_distance(entity.lat, entity.lon, center_lat, center_lon)

    if dist_to_center > 50:  # More than 50m from center, head back
        entity.hdg = bearing_to(entity.lat, entity.lon, center_lat, center_lon)
        entity.hdg += random.uniform(-20, 20)  # Some variation
    else:
        # Mill around slowly with random turns
        entity.hdg = (entity.hdg + random.uniform(-30, 30)) % 360

    # Slow speed during gathering (1-3 knots)
    entity.spd = random.uniform(1, 3)

    # Move
    distance_m = entity.spd * 0.514444 * dt  # knots to m/s
    entity.lat, entity.lon = move_point(entity.lat, entity.lon, entity.hdg, distance_m)


def run_offline_simulation(args, entities: List[SimulatedEntity], simulator: 'SailingSimulator',
                           course_waypoints: Optional[List[Tuple[float, float]]],
                           mark_colors: Dict[int, str], wind_direction: float,
                           start_loc: Tuple[float, float], one_hz: bool = False):
    """Run offline simulation and write to log file."""

    # Parse start date
    if args.date:
        try:
            start_dt = datetime.strptime(args.date, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                start_dt = datetime.strptime(args.date, "%Y-%m-%d")
            except ValueError:
                print(f"Error: Invalid date format '{args.date}'. Use 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'")
                return
    else:
        start_dt = datetime.now()

    # Determine output filename
    if args.output:
        output_file = args.output
    else:
        output_file = start_dt.strftime("%Y_%m_%d") + ".jsonl"

    # Calculate gathering area
    if course_waypoints and len(course_waypoints) >= 2:
        gathering_center, downwind_bearing = get_gathering_area(course_waypoints)
    else:
        gathering_center = start_loc
        downwind_bearing = 0

    # Parse mark order
    if course_waypoints:
        mark_order = parse_mark_order(args.mark_order, len(course_waypoints))
    else:
        mark_order = []

    sailors = [e for e in entities if e.role == "sailor"]

    # Timing constants (in simulated seconds)
    PRE_RACE_DURATION = 120    # 2 minutes
    POST_RACE_DURATION = 300   # 5 minutes
    REPORT_INTERVAL = 10       # 10 seconds between log entries
    SIM_INTERVAL = 1 if one_hz else 10  # 1Hz mode simulates every second

    print(f"\nOffline simulation:")
    print(f"  Output file: {output_file}")
    print(f"  Start time: {start_dt}")
    print(f"  Num races: {args.num_races}")
    print(f"  Mark order: {mark_order}")
    print(f"  Time scale: {args.time_scale}x")
    print(f"  Mode: {'1Hz (pos arrays)' if one_hz else '10s intervals'}")
    print()

    # Initialize all sailors in gathering area
    for entity in sailors:
        entity.race_state = RaceState.PRE_RACE
        entity.has_finished = False
        entity.mark_order = list(mark_order)
        entity.mark_order_idx = 0
        # Move to gathering area
        offset_bearing = random.uniform(0, 360)
        offset_dist = random.uniform(0, 40)
        entity.lat, entity.lon = move_point(gathering_center[0], gathering_center[1],
                                            offset_bearing, offset_dist)

    current_ts = int(start_dt.timestamp())
    total_entries = 0

    # Position buffers for 1Hz mode (entity_id -> list of (ts, lat, lon, spd))
    pos_buffers: Dict[str, List[Tuple[int, float, float, float]]] = {e.id: [] for e in entities}

    def write_positions(f, entities, current_ts, force=False):
        """Write positions - either immediately or buffered for 1Hz mode."""
        nonlocal total_entries
        if one_hz:
            for entity in entities:
                # Add current position to buffer
                pos_buffers[entity.id].append((current_ts, entity.lat, entity.lon, entity.spd))
                # Write when buffer has 10 positions or forced
                if len(pos_buffers[entity.id]) >= 10 or force:
                    if pos_buffers[entity.id]:
                        write_log_entry_1hz(f, entity, pos_buffers[entity.id])
                        total_entries += 1
                        pos_buffers[entity.id] = []
        else:
            for entity in entities:
                write_log_entry(f, entity, current_ts)
                total_entries += 1

    with open(output_file, 'w') as f:
        for race_num in range(1, args.num_races + 1):
            print(f"Race {race_num}/{args.num_races}...")

            # Reset sailors for new race
            for entity in sailors:
                entity.race_state = RaceState.PRE_RACE
                entity.has_finished = False
                entity.mark_order_idx = 0
                entity.current_waypoint_idx = 0
                entity.race_wp_idx = 0
                entity.race_waypoints = []

            # Clear position buffers for new race
            for eid in pos_buffers:
                pos_buffers[eid] = []

            # === PRE-RACE PHASE ===
            print(f"  Pre-race gathering ({PRE_RACE_DURATION}s)...", end="", flush=True)
            phase_start = current_ts
            while current_ts - phase_start < PRE_RACE_DURATION:
                # Update all entities
                for entity in entities:
                    if entity.role == "sailor":
                        update_gathering_sailor(entity, gathering_center, downwind_bearing, SIM_INTERVAL)
                    elif entity.role == "support":
                        simulator.update_support(entity, SIM_INTERVAL, sailors)
                    else:
                        simulator.update_spectator(entity, SIM_INTERVAL)

                # Write positions (buffered for 1Hz, immediate otherwise)
                write_positions(f, entities, current_ts)
                current_ts += SIM_INTERVAL

            # Flush remaining buffered positions at end of phase
            if one_hz:
                write_positions(f, entities, current_ts, force=True)
            print(" done")

            # === RACING PHASE ===
            # Set all sailors to racing state and build race waypoints with roundings
            for entity in sailors:
                entity.race_state = RaceState.RACING
                if course_waypoints and mark_order:
                    # Build waypoints with proper mark roundings
                    start_pos = (entity.lat, entity.lon)
                    entity.race_waypoints = build_race_waypoints(
                        start_pos, course_waypoints, mark_order, mark_colors
                    )
                    entity.race_wp_idx = 0
                    if entity.race_waypoints:
                        entity.target_lat, entity.target_lon = entity.race_waypoints[0]
                        entity.course_waypoints = list(entity.race_waypoints)
                        entity.current_waypoint_idx = 0

            print(f"  Racing...", end="", flush=True)
            race_start_ts = current_ts

            while True:
                # Update all entities
                for entity in entities:
                    if entity.role == "sailor" and not entity.has_finished:
                        # Racing sailor
                        simulator.update_sailor(entity, SIM_INTERVAL)

                        # Check if reached current waypoint (using race_waypoints with roundings)
                        if entity.race_waypoints:
                            target = entity.race_waypoints[entity.race_wp_idx]
                            dist = haversine_distance(entity.lat, entity.lon, target[0], target[1])

                            if dist < 35:  # Within 35m of waypoint
                                entity.race_wp_idx += 1
                                if entity.race_wp_idx >= len(entity.race_waypoints):
                                    # Finished the course!
                                    entity.has_finished = True
                                    entity.race_state = RaceState.POST_RACE
                                else:
                                    # Move to next waypoint
                                    next_wp = entity.race_waypoints[entity.race_wp_idx]
                                    entity.current_waypoint_idx = entity.race_wp_idx
                                    entity.target_lat, entity.target_lon = next_wp

                    elif entity.role == "sailor" and entity.has_finished:
                        # Finished sailor mills around
                        update_gathering_sailor(entity, gathering_center, downwind_bearing, SIM_INTERVAL)
                    elif entity.role == "support":
                        simulator.update_support(entity, SIM_INTERVAL, sailors)
                    else:
                        simulator.update_spectator(entity, SIM_INTERVAL)

                # Write positions (buffered for 1Hz, immediate otherwise)
                write_positions(f, entities, current_ts)
                current_ts += SIM_INTERVAL

                # Check if all sailors finished
                if all(s.has_finished for s in sailors):
                    break

                # Safety limit - 2 hours max per race
                if current_ts - race_start_ts > 7200:
                    print(f"\n  Warning: Race timeout after 2 hours")
                    for s in sailors:
                        s.has_finished = True
                    break

            # Flush remaining buffered positions at end of phase
            if one_hz:
                write_positions(f, entities, current_ts, force=True)

            race_duration = current_ts - race_start_ts
            print(f" done ({race_duration}s)")

            # === POST-RACE PHASE ===
            if race_num < args.num_races:  # Only if not the last race
                print(f"  Post-race gathering ({POST_RACE_DURATION}s)...", end="", flush=True)

                # Move all sailors back to gathering area
                for entity in sailors:
                    entity.race_state = RaceState.POST_RACE
                    offset_bearing = random.uniform(0, 360)
                    offset_dist = random.uniform(0, 40)
                    entity.lat, entity.lon = move_point(gathering_center[0], gathering_center[1],
                                                        offset_bearing, offset_dist)

                phase_start = current_ts
                while current_ts - phase_start < POST_RACE_DURATION:
                    for entity in entities:
                        if entity.role == "sailor":
                            update_gathering_sailor(entity, gathering_center, downwind_bearing, SIM_INTERVAL)
                        elif entity.role == "support":
                            simulator.update_support(entity, SIM_INTERVAL, sailors)
                        else:
                            simulator.update_spectator(entity, SIM_INTERVAL)

                    # Write positions (buffered for 1Hz, immediate otherwise)
                    write_positions(f, entities, current_ts)
                    current_ts += SIM_INTERVAL

                # Flush remaining buffered positions at end of phase
                if one_hz:
                    write_positions(f, entities, current_ts, force=True)
                print(" done")

    # Calculate total simulated time
    end_dt = datetime.fromtimestamp(current_ts)
    total_sim_time = current_ts - int(start_dt.timestamp())

    # Create gzipped version for efficient serving
    gz_file = output_file + '.gz'
    with open(output_file, 'rb') as f_in:
        with gzip.open(gz_file, 'wb') as f_out:
            f_out.writelines(f_in)

    # Generate summary file by scanning the log we just wrote
    summary_file = output_file.replace('.jsonl', '_summary.json')
    sailors_summary = {}
    start_ts = None
    end_ts = None
    point_count = 0

    with open(output_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = entry.get('ts')
                sailor_id = entry.get('id')
                if ts is None or sailor_id is None:
                    continue

                point_count += 1
                if start_ts is None or ts < start_ts:
                    start_ts = ts
                if end_ts is None or ts > end_ts:
                    end_ts = ts

                if sailor_id not in sailors_summary:
                    sailors_summary[sailor_id] = {
                        'points': 0,
                        'first_ts': ts,
                        'last_ts': ts
                    }
                sailors_summary[sailor_id]['points'] += 1
                if ts < sailors_summary[sailor_id]['first_ts']:
                    sailors_summary[sailor_id]['first_ts'] = ts
                if ts > sailors_summary[sailor_id]['last_ts']:
                    sailors_summary[sailor_id]['last_ts'] = ts
            except json.JSONDecodeError:
                continue

    # Build summary structure
    log_filename = os.path.basename(output_file)
    date_str = start_dt.strftime("%Y_%m_%d")
    summary = {
        'date': date_str,
        'generated': time.time(),
        'generated_iso': datetime.now().isoformat(),
        'logs': [{
            'file': log_filename,
            'index': 0,
            'start_ts': start_ts,
            'end_ts': end_ts,
            'point_count': point_count,
            'sailors': sailors_summary
        }]
    }

    with open(summary_file, 'w') as f:
        json.dump(summary, f, indent=2)

    print()
    print(f"Simulation complete:")
    print(f"  Output: {output_file}")
    print(f"  Compressed: {gz_file}")
    print(f"  Summary: {summary_file}")
    print(f"  Entries written: {total_entries}")
    print(f"  Simulated time: {total_sim_time // 3600}h {(total_sim_time % 3600) // 60}m")
    print(f"  Time range: {start_dt} to {end_dt}")


def main():
    parser = argparse.ArgumentParser(description="Multi-entity tracker simulator")
    parser.add_argument("-H", "--host", default="127.0.0.1", help="Server host")
    parser.add_argument("-p", "--port", type=int, default=41234, help="Server port")
    parser.add_argument("--num-sailors", type=int, default=5, help="Number of sailors")
    parser.add_argument("--num-support", type=int, default=1, help="Number of support boats")
    parser.add_argument("--num-spectators", type=int, default=2, help="Number of spectators")
    parser.add_argument("--start-loc", type=str, default="-36.8485,174.7633",
                        help="Start location as lat,lon (fallback if no course URL)")
    parser.add_argument("--end-loc", type=str, default="-36.8385,174.7733",
                        help="End location as lat,lon (fallback if no course URL)")
    parser.add_argument("-d", "--delay", type=float, default=10.0,
                        help="Delay between position reports (seconds)")
    parser.add_argument("--duration", type=int, default=0,
                        help="Duration in seconds (0 = run forever)")
    parser.add_argument("--assist", type=str, default="",
                        help="Entity ID to set assist flag (e.g., S03)")
    parser.add_argument("--password", type=str, default="",
                        help="Password to include in packets")
    parser.add_argument("--eid", type=int, default=1,
                        help="Event ID to include in packets (default: 1)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # New arguments
    parser.add_argument("--course", type=str, default="",
                        help="Course source: URL or local JSON file (default: http://HOST:PORT/api/event/EID/course)")
    parser.add_argument("--no-course", action="store_true",
                        help="Don't load course from server, use start/end locations only")
    parser.add_argument("--wind-direction", type=float, default=45,
                        help="Wind direction in degrees (0=N, 90=E, default=45=NE)")
    parser.add_argument("--laps", type=int, default=0,
                        help="Number of laps (0 = infinite)")
    parser.add_argument("--coastline", type=str, default="",
                        help="GeoJSON coastline file for land avoidance")
    parser.add_argument("--no-land-avoidance", action="store_true",
                        help="Disable land avoidance checking")

    # Offline log generation
    parser.add_argument("--offline", action="store_true",
                        help="Write directly to log file instead of sending UDP packets")
    parser.add_argument("--output", type=str, default="",
                        help="Output filename (default: YYYY_MM_DD.jsonl from --date)")
    parser.add_argument("--date", type=str, default="",
                        help="Start date/time for log, e.g., '2026-01-15 10:00' (default: now)")
    parser.add_argument("--num-races", type=int, default=1,
                        help="Number of races to simulate (default: 1)")
    parser.add_argument("--mark-order", type=str, default="",
                        help="Mark rounding order, e.g., '1,2,3,1,2,1' (default: all marks in sequence)")
    parser.add_argument("--time-scale", type=float, default=100.0,
                        help="Speed multiplier for offline mode (default: 100)")
    parser.add_argument("--one-hz", action="store_true",
                        help="Generate 1Hz data with pos arrays (10 positions per entry)")
    parser.add_argument("--speed", type=float, default=12.0,
                        help="Average sailor speed in knots (default: 12, std dev: 20%%)")

    args = parser.parse_args()

    # Parse fallback locations
    start_loc = tuple(map(float, args.start_loc.split(",")))
    end_loc = tuple(map(float, args.end_loc.split(",")))

    # Load course from URL or file
    course_data = None
    course_waypoints = None
    mark_colors = {}
    if not args.no_course:
        # Determine course source
        if args.course:
            course_source = args.course
        else:
            # Default: use server API endpoint
            protocol = "https" if args.port == 443 else "http"
            course_source = f"{protocol}://{args.host}:{args.port}/api/event/{args.eid}/course"

        course_data = load_course(course_source)
        if course_data:
            course_waypoints = course_data.waypoints
            mark_colors = course_data.mark_colors
            # Update start/end from course
            start_loc = course_waypoints[0]
            end_loc = course_waypoints[-1]

    # Load coastline data
    coastline = None
    if args.coastline and not args.no_land_avoidance:
        coastline = load_coastline(args.coastline)
        if coastline:
            print(f"Loaded coastline with {len(coastline.land_polygons)} polygons")

    print(f"Starting simulation:")
    print(f"  Event ID: {args.eid}")
    print(f"  Sailors: {args.num_sailors}")
    print(f"  Support: {args.num_support}")
    print(f"  Spectators: {args.num_spectators}")
    if course_waypoints:
        print(f"  Course: {len(course_waypoints)} waypoints")
        for i, (lat, lon) in enumerate(course_waypoints):
            print(f"    [{i}] {lat:.5f}, {lon:.5f}")
    else:
        print(f"  Start: {start_loc}")
        print(f"  End: {end_loc}")
    print(f"  Wind direction: {args.wind_direction}° (from {'N' if args.wind_direction == 0 else 'NE' if args.wind_direction == 45 else 'E' if args.wind_direction == 90 else f'{args.wind_direction}°'})")
    print(f"  Laps: {'infinite' if args.laps == 0 else args.laps}")
    print(f"  Update interval: {args.delay}s")
    if coastline:
        print(f"  Land avoidance: enabled")
    print()

    # Create entities
    entities = create_entities(
        args.num_sailors, args.num_support, args.num_spectators,
        start_loc, end_loc, course_waypoints, avg_speed=args.speed
    )

    # Set assist if requested
    if args.assist:
        for e in entities:
            if e.id == args.assist:
                e.assist = True
                print(f"*** {e.id} has ASSIST flag set ***")

    # For offline mode with races, calculate wind from course
    wind_direction = args.wind_direction
    if args.offline and course_waypoints and len(course_waypoints) >= 2:
        wind_direction = calculate_wind_from_course(course_waypoints)
        print(f"  Wind direction (auto): {wind_direction:.0f}° (first leg into wind)")

    # Create simulator
    simulator = SailingSimulator(start_loc, end_loc,
                                  wind_direction=wind_direction,
                                  coastline=coastline,
                                  num_laps=args.laps)

    # Handle offline mode
    if args.offline:
        run_offline_simulation(args, entities, simulator, course_waypoints, mark_colors,
                               wind_direction, start_loc, one_hz=args.one_hz)
        return

    # Create socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)

    sailors = [e for e in entities if e.role == "sailor"]

    start_time = time.time()
    last_update = start_time
    update_count = 0

    # Separate 1Hz and regular entities
    entities_1hz = [e for e in entities if e.is_1hz]
    entities_regular = [e for e in entities if not e.is_1hz]

    hz1_count = len(entities_1hz)
    regular_count = len(entities_regular)
    print(f"  1Hz entities: {hz1_count}")
    print(f"  Regular entities: {regular_count}")
    print()

    try:
        while True:
            current_time = time.time()
            dt = current_time - last_update

            # Check duration limit
            if args.duration > 0 and (current_time - start_time) >= args.duration:
                print(f"\nDuration limit reached ({args.duration}s)")
                break

            # Update and accumulate positions for 1Hz entities (10 sub-updates)
            batch_size = int(args.delay)  # Number of 1Hz samples to collect
            for step in range(batch_size):
                ts = int(current_time - args.delay + step + 1)  # Timestamps spread over interval
                for entity in entities_1hz:
                    # Update position with 1-second dt
                    if entity.role == "sailor":
                        simulator.update_sailor(entity, 1.0)
                    elif entity.role == "support":
                        simulator.update_support(entity, 1.0, sailors)
                    else:
                        simulator.update_spectator(entity, 1.0)

                    # Accumulate position in buffer (ts, lat, lon, spd)
                    entity.pos_buffer.append((ts, entity.lat, entity.lon, entity.spd))

                    # Update heart rate occasionally (varies slowly)
                    if random.random() < 0.1:
                        entity.heart_rate = max(50, min(180, entity.heart_rate + random.randint(-3, 5)))

            # Update regular entities with full dt
            for entity in entities_regular:
                if entity.role == "sailor":
                    simulator.update_sailor(entity, dt)
                elif entity.role == "support":
                    simulator.update_support(entity, dt, sailors)
                else:
                    simulator.update_spectator(entity, dt)

            # Common updates for all entities
            for entity in entities:
                # Simulate battery drain (very slow)
                if random.random() < 0.01:
                    entity.battery = max(5, entity.battery - 1)

                # Simulate signal fluctuation
                entity.signal = max(0, min(4, entity.signal + random.choice([-1, 0, 0, 0, 1])))

            last_update = current_time

            # Send packets
            acked = 0

            # Send 1Hz batch packets
            for entity in entities_1hz:
                if entity.pos_buffer:  # Only send if we have positions
                    if send_packet_1hz(sock, args.host, args.port, entity, args.password, args.eid):
                        acked += 1

            # Send regular packets
            for entity in entities_regular:
                if send_packet(sock, args.host, args.port, entity, args.password, args.eid):
                    acked += 1

            update_count += 1

            if args.verbose:
                print(f"[{update_count}] Sent {len(entities)} packets, {acked} ACKed")
                for e in entities:
                    status = "⚠ ASSIST" if e.assist else ""
                    mode = " [1Hz]" if e.is_1hz else ""
                    hr_str = f" hr={e.heart_rate}" if e.is_1hz else ""
                    lap_info = f" lap={e.current_lap} wp={e.current_waypoint_idx}" if e.course_waypoints else ""
                    print(f"  {e.id} ({e.role}{mode}): {e.lat:.5f}, {e.lon:.5f} "
                          f"spd={e.spd:.1f}kn hdg={e.hdg:.0f}° bat={e.battery}%{hr_str}{lap_info} {status}")
            else:
                elapsed = int(current_time - start_time)
                assist_count = sum(1 for e in entities if e.assist)
                assist_str = f" [{assist_count} ASSIST]" if assist_count else ""
                print(f"[{elapsed:4d}s] Update {update_count}: {acked}/{len(entities)} ACKed "
                      f"({hz1_count} 1Hz, {regular_count} reg){assist_str}", end="\r")

            time.sleep(args.delay)

    except KeyboardInterrupt:
        print("\n\nSimulation stopped by user")
    finally:
        sock.close()

    print(f"\nTotal updates sent: {update_count}")


if __name__ == "__main__":
    main()
