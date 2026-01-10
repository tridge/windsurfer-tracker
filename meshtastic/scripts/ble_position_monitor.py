#!/usr/bin/env python3
"""
Connect to a Meshtastic node via Bluetooth LE and print GPS positions continuously.

Usage:
    python3 ble_position_monitor.py [device_name_or_address]

Examples:
    python3 ble_position_monitor.py              # Scan and connect to first Meshtastic device
    python3 ble_position_monitor.py AT2_6830     # Connect by name
    python3 ble_position_monitor.py F7:24:F5:A3:68:30  # Connect by address
"""

import sys
import time
import argparse
from datetime import datetime

import meshtastic
import meshtastic.ble_interface
from pubsub import pub


class PositionMonitor:
    def __init__(self, device=None):
        self.device = device
        self.interface = None
        self.last_position = None

    def on_receive(self, packet, interface):
        """Handle received packets."""
        decoded = packet.get('decoded', {})
        portnum = decoded.get('portnum')

        if portnum == 'POSITION_APP':
            pos = decoded.get('position', {})
            self.print_position(packet, pos)
        elif portnum == 'TELEMETRY_APP':
            telemetry = decoded.get('telemetry', {})
            self.print_telemetry(packet, telemetry)

    def print_position(self, packet, pos):
        """Print position data."""
        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        from_id = packet.get('fromId', '?')

        lat = pos.get('latitude')
        lon = pos.get('longitude')
        alt = pos.get('altitude')
        speed = pos.get('groundSpeed')  # m/s
        heading = pos.get('groundTrack')  # degrees * 1e5
        sats = pos.get('satsInView')
        pdop = pos.get('PDOP')

        # Convert speed to knots if available
        speed_kts = speed * 1.94384 if speed else None

        # Convert heading from 1e5 format
        heading_deg = heading / 1e5 if heading else None

        print(f"[{ts}] POS from {from_id}:")
        print(f"  Lat: {lat:.7f}  Lon: {lon:.7f}" if lat and lon else "  No fix")
        if alt:
            print(f"  Alt: {alt}m")
        if speed_kts is not None:
            print(f"  Speed: {speed_kts:.1f} kts ({speed:.1f} m/s)")
        if heading_deg is not None:
            print(f"  Heading: {heading_deg:.1f}°")
        if sats:
            print(f"  Sats: {sats}  PDOP: {pdop}")
        print()

        self.last_position = pos

    def print_telemetry(self, packet, telemetry):
        """Print telemetry data."""
        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        from_id = packet.get('fromId', '?')

        device_metrics = telemetry.get('deviceMetrics', {})
        if device_metrics:
            battery = device_metrics.get('batteryLevel')
            voltage = device_metrics.get('voltage')
            if battery is not None:
                print(f"[{ts}] TELEMETRY from {from_id}: Battery {battery}% ({voltage:.2f}V)")

    def on_connection(self, interface, topic=pub.AUTO_TOPIC):
        """Handle connection established."""
        print(f"Connected to Meshtastic node!")
        node = interface.getMyNodeInfo()
        if node:
            user = node.get('user', {})
            print(f"  Name: {user.get('longName')} ({user.get('shortName')})")
            print(f"  ID: {user.get('id')}")
            print(f"  Hardware: {user.get('hwModel')}")
        print()
        print("Listening for position updates... (Ctrl+C to exit)")
        print()

    def connect(self):
        """Connect to the Meshtastic device via BLE."""
        # Subscribe to events
        pub.subscribe(self.on_receive, 'meshtastic.receive')
        pub.subscribe(self.on_connection, 'meshtastic.connection.established')

        print(f"Connecting via BLE to: {self.device or 'first available device'}...")

        try:
            self.interface = meshtastic.ble_interface.BLEInterface(self.device)
            return True
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

    def run(self):
        """Run the monitor loop."""
        if not self.connect():
            return 1

        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nExiting...")
        finally:
            if self.interface:
                self.interface.close()

        return 0


def main():
    parser = argparse.ArgumentParser(
        description='Monitor GPS positions from a Meshtastic node via BLE'
    )
    parser.add_argument(
        'device',
        nargs='?',
        default=None,
        help='Device name or BLE address (default: scan for first device)'
    )
    parser.add_argument(
        '--scan',
        action='store_true',
        help='Just scan for devices, don\'t connect'
    )

    args = parser.parse_args()

    if args.scan:
        print("Scanning for Meshtastic BLE devices...")
        import asyncio
        from bleak import BleakScanner

        async def scan():
            devices = await BleakScanner.discover(timeout=10)
            mesh_devices = []
            for d in devices:
                # Meshtastic devices advertise specific service UUID
                if d.name and ('mesh' in d.name.lower() or d.name.startswith('AT') or 'Meshtastic' in d.name):
                    mesh_devices.append(d)
                    print(f"  Found: {d.name} - {d.address}")
            if not mesh_devices:
                print("  No Meshtastic devices found")
            return mesh_devices

        asyncio.run(scan())
        return 0

    monitor = PositionMonitor(args.device)
    return monitor.run()


if __name__ == '__main__':
    sys.exit(main())
