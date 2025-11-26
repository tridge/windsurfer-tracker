#!/usr/bin/env python3
"""
Test client - simulates multiple sailors, support boats, and spectators.
Sailors move between start and end locations with realistic tacking behavior.
"""

import socket
import json
import time
import argparse
import random
import math
import subprocess
from dataclasses import dataclass
from typing import List, Tuple


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
    
    # Movement parameters
    base_speed: float = 10.0
    speed_variance: float = 3.0
    
    def __post_init__(self):
        self.target_lat = self.lat
        self.target_lon = self.lon


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


class SailingSimulator:
    """Simulates realistic sailing movement with tacking"""
    
    WIND_DIRECTION = 45  # Wind coming from NE (degrees)
    TACK_ANGLE = 45      # Angle to wind when close-hauled
    
    def __init__(self, start_loc: Tuple[float, float], end_loc: Tuple[float, float]):
        self.start_lat, self.start_lon = start_loc
        self.end_lat, self.end_lon = end_loc
        
    def update_sailor(self, entity: SimulatedEntity, dt: float):
        """Update sailor position with tacking behavior"""
        # Calculate bearing to target
        target_bearing = bearing_to(entity.lat, entity.lon, entity.target_lat, entity.target_lon)
        distance = haversine_distance(entity.lat, entity.lon, entity.target_lat, entity.target_lon)
        
        # Check if reached target, swap start/end
        if distance < 50:  # Within 50m
            if abs(entity.target_lat - self.end_lat) < 0.0001:
                entity.target_lat, entity.target_lon = self.start_lat, self.start_lon
            else:
                entity.target_lat, entity.target_lon = self.end_lat, self.end_lon
            target_bearing = bearing_to(entity.lat, entity.lon, entity.target_lat, entity.target_lon)
        
        # Determine if we need to tack (sailing into wind)
        wind_angle = (target_bearing - self.WIND_DIRECTION + 360) % 360
        
        if wind_angle < self.TACK_ANGLE or wind_angle > (360 - self.TACK_ANGLE):
            # Need to tack - can't sail directly into wind
            entity.tack_timer -= dt
            
            if entity.tack_timer <= 0:
                # Time to tack
                entity.on_starboard = not entity.on_starboard
                entity.tack_timer = random.uniform(15, 45)  # Tack every 15-45 seconds
            
            # Sail at angle to wind
            if entity.on_starboard:
                entity.hdg = (self.WIND_DIRECTION + self.TACK_ANGLE) % 360
            else:
                entity.hdg = (self.WIND_DIRECTION - self.TACK_ANGLE + 360) % 360
                
            # Slower when beating
            entity.spd = entity.base_speed * 0.7 + random.uniform(-1, 1)
            
        elif 60 < wind_angle < 120 or 240 < wind_angle < 300:
            # Reaching - fastest point of sail
            entity.hdg = target_bearing + random.uniform(-10, 10)
            entity.spd = entity.base_speed * 1.2 + random.uniform(-1, 2)
            entity.tack_timer = 0
            
        else:
            # Running or broad reach
            entity.hdg = target_bearing + random.uniform(-15, 15)
            entity.spd = entity.base_speed * 0.9 + random.uniform(-1, 1)
            entity.tack_timer = 0
        
        # Ensure heading is in valid range
        entity.hdg = (entity.hdg + 360) % 360
        
        # Ensure speed is positive
        entity.spd = max(0.5, entity.spd)
        
        # Move the entity
        distance_m = entity.spd * 0.514444 * dt  # knots to m/s, then * time
        entity.lat, entity.lon = move_point(entity.lat, entity.lon, entity.hdg, distance_m)
        
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
        entity.lat, entity.lon = move_point(entity.lat, entity.lon, entity.hdg, distance_m)
        
    def update_spectator(self, entity: SimulatedEntity, dt: float):
        """Update spectator - mostly stationary with drift"""
        # Slow random drift
        entity.hdg = (entity.hdg + random.uniform(-5, 5)) % 360
        entity.spd = random.uniform(0, 0.5)
        
        distance_m = entity.spd * 0.514444 * dt
        entity.lat, entity.lon = move_point(entity.lat, entity.lon, entity.hdg, distance_m)


def create_entities(num_sailors: int, num_support: int, num_spectators: int,
                    start_loc: Tuple[float, float], end_loc: Tuple[float, float]) -> List[SimulatedEntity]:
    """Create all simulated entities spread along the course"""
    entities = []

    # Spread sailors along the course from start to end
    for i in range(num_sailors):
        # Position along course (0.0 = start, 1.0 = end)
        progress = i / max(1, num_sailors - 1) if num_sailors > 1 else 0.5
        base_lat = start_loc[0] + (end_loc[0] - start_loc[0]) * progress
        base_lon = start_loc[1] + (end_loc[1] - start_loc[1]) * progress
        # Add some random spread perpendicular to course
        lat = base_lat + random.uniform(-0.002, 0.002)
        lon = base_lon + random.uniform(-0.002, 0.002)
        # Set target based on position - those closer to start head to end, others to start
        if progress < 0.5:
            target_lat, target_lon = end_loc[0], end_loc[1]
        else:
            target_lat, target_lon = start_loc[0], start_loc[1]
        entity = SimulatedEntity(
            id=f"Test{i+1:02d}",
            role="sailor",
            lat=lat,
            lon=lon,
            target_lat=target_lat,
            target_lon=target_lon,
            base_speed=random.uniform(8, 14),
            battery=random.randint(70, 100),
            signal=random.randint(2, 4),
            on_starboard=random.choice([True, False]),
            tack_timer=random.uniform(5, 30)
        )
        entities.append(entity)

    # Spread support boats along the course
    for i in range(num_support):
        # Position along course (evenly distributed)
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

    # Spectators positioned along the side of the course
    mid_lat = (start_loc[0] + end_loc[0]) / 2
    mid_lon = (start_loc[1] + end_loc[1]) / 2
    for i in range(num_spectators):
        # Spread along course with offset to the side
        progress = (i + 0.5) / num_spectators if num_spectators > 0 else 0.5
        base_lat = start_loc[0] + (end_loc[0] - start_loc[0]) * progress
        base_lon = start_loc[1] + (end_loc[1] - start_loc[1]) * progress
        lat = base_lat + random.uniform(-0.001, 0.001)
        lon = base_lon + random.uniform(0.002, 0.005)  # Offset to east
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


def send_packet(sock: socket.socket, host: str, port: int, entity: SimulatedEntity, password: str = "") -> bool:
    """Send position packet and wait for ACK"""
    entity.seq += 1

    packet = {
        "id": entity.id,
        "sq": entity.seq,
        "ts": int(time.time()),
        "lat": round(entity.lat, 6),
        "lon": round(entity.lon, 6),
        "spd": round(entity.spd, 1),
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
        return True
    except socket.timeout:
        return False


def main():
    parser = argparse.ArgumentParser(description="Multi-entity tracker simulator")
    parser.add_argument("-H", "--host", default="127.0.0.1", help="Server host")
    parser.add_argument("-p", "--port", type=int, default=41234, help="Server port")
    parser.add_argument("--num-sailors", type=int, default=5, help="Number of sailors")
    parser.add_argument("--num-support", type=int, default=1, help="Number of support boats")
    parser.add_argument("--num-spectators", type=int, default=2, help="Number of spectators")
    parser.add_argument("--start-loc", type=str, default="-36.8485,174.7633",
                        help="Start location as lat,lon")
    parser.add_argument("--end-loc", type=str, default="-36.8385,174.7733",
                        help="End location as lat,lon")
    parser.add_argument("-d", "--delay", type=float, default=10.0,
                        help="Delay between position reports (seconds)")
    parser.add_argument("--duration", type=int, default=0,
                        help="Duration in seconds (0 = run forever)")
    parser.add_argument("--assist", type=str, default="",
                        help="Entity ID to set assist flag (e.g., S03)")
    parser.add_argument("--password", type=str, default="",
                        help="Password to include in packets")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    
    args = parser.parse_args()
    
    # Parse locations
    start_loc = tuple(map(float, args.start_loc.split(",")))
    end_loc = tuple(map(float, args.end_loc.split(",")))
    
    print(f"Starting simulation:")
    print(f"  Sailors: {args.num_sailors}")
    print(f"  Support: {args.num_support}")
    print(f"  Spectators: {args.num_spectators}")
    print(f"  Start: {start_loc}")
    print(f"  End: {end_loc}")
    print(f"  Update interval: {args.delay}s")
    print()
    
    # Create entities
    entities = create_entities(
        args.num_sailors, args.num_support, args.num_spectators,
        start_loc, end_loc
    )
    
    # Set assist if requested
    if args.assist:
        for e in entities:
            if e.id == args.assist:
                e.assist = True
                print(f"*** {e.id} has ASSIST flag set ***")
    
    # Create simulator
    simulator = SailingSimulator(start_loc, end_loc)
    
    # Create socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    
    sailors = [e for e in entities if e.role == "sailor"]
    
    start_time = time.time()
    last_update = start_time
    update_count = 0
    
    try:
        while True:
            current_time = time.time()
            dt = current_time - last_update
            
            # Check duration limit
            if args.duration > 0 and (current_time - start_time) >= args.duration:
                print(f"\nDuration limit reached ({args.duration}s)")
                break
            
            # Update entity positions
            for entity in entities:
                if entity.role == "sailor":
                    simulator.update_sailor(entity, dt)
                elif entity.role == "support":
                    simulator.update_support(entity, dt, sailors)
                else:
                    simulator.update_spectator(entity, dt)
                
                # Simulate battery drain (very slow)
                if random.random() < 0.01:
                    entity.battery = max(5, entity.battery - 1)
                
                # Simulate signal fluctuation
                entity.signal = max(0, min(4, entity.signal + random.choice([-1, 0, 0, 0, 1])))
            
            last_update = current_time
            
            # Send packets
            acked = 0
            for entity in entities:
                if send_packet(sock, args.host, args.port, entity, args.password):
                    acked += 1
            
            update_count += 1
            
            if args.verbose:
                print(f"[{update_count}] Sent {len(entities)} packets, {acked} ACKed")
                for e in entities:
                    status = "⚠ ASSIST" if e.assist else ""
                    print(f"  {e.id} ({e.role}): {e.lat:.5f}, {e.lon:.5f} "
                          f"spd={e.spd:.1f}kn hdg={e.hdg:.0f}° bat={e.battery}% {status}")
            else:
                elapsed = int(current_time - start_time)
                assist_count = sum(1 for e in entities if e.assist)
                assist_str = f" [{assist_count} ASSIST]" if assist_count else ""
                print(f"[{elapsed:4d}s] Update {update_count}: {acked}/{len(entities)} ACKed{assist_str}", end="\r")
            
            time.sleep(args.delay)
            
    except KeyboardInterrupt:
        print("\n\nSimulation stopped by user")
    finally:
        sock.close()
        
    print(f"\nTotal updates sent: {update_count}")


if __name__ == "__main__":
    main()
