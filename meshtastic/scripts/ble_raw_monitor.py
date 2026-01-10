#!/usr/bin/env python3
"""
Connect to a Meshtastic node via raw Bluetooth LE and print GPS positions.
Uses bleak directly to avoid meshtastic library BLE issues on Linux.

Usage:
    python3 ble_raw_monitor.py [device_name_or_address]
"""

import sys
import asyncio
import struct
from datetime import datetime

# Meshtastic BLE UUIDs
SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"

# Import protobuf definitions
try:
    from meshtastic import mesh_pb2, portnums_pb2, telemetry_pb2
    HAS_PROTOBUF = True
except ImportError:
    HAS_PROTOBUF = False
    print("Warning: meshtastic protobuf not available, raw hex output only")


class RawBLEMonitor:
    def __init__(self, device_address):
        self.device_address = device_address
        self.client = None
        self.packet_count = 0

    def decode_position(self, data):
        """Decode a position protobuf."""
        if not HAS_PROTOBUF:
            return None

        try:
            pos = mesh_pb2.Position()
            pos.ParseFromString(data)
            return {
                'latitude': pos.latitude_i / 1e7 if pos.latitude_i else None,
                'longitude': pos.longitude_i / 1e7 if pos.longitude_i else None,
                'altitude': pos.altitude if pos.altitude else None,
                'speed': pos.ground_speed if pos.ground_speed else None,  # m/s
                'heading': pos.ground_track / 1e5 if pos.ground_track else None,  # degrees
                'sats': pos.sats_in_view if pos.sats_in_view else None,
                'pdop': pos.PDOP if pos.PDOP else None,
            }
        except Exception as e:
            print(f"  Position decode error: {e}")
            return None

    def decode_telemetry(self, data):
        """Decode a telemetry protobuf."""
        if not HAS_PROTOBUF:
            return None

        try:
            tel = telemetry_pb2.Telemetry()
            tel.ParseFromString(data)
            result = {}
            if tel.HasField('device_metrics'):
                dm = tel.device_metrics
                result['battery'] = dm.battery_level if dm.battery_level else None
                result['voltage'] = dm.voltage if dm.voltage else None
            return result if result else None
        except Exception as e:
            print(f"  Telemetry decode error: {e}")
            return None

    def handle_fromradio(self, data):
        """Handle data from FromRadio characteristic."""
        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]

        if not HAS_PROTOBUF:
            print(f"[{ts}] Raw data ({len(data)} bytes): {data.hex()}")
            return

        try:
            fr = mesh_pb2.FromRadio()
            fr.ParseFromString(bytes(data))

            if fr.HasField('packet'):
                pkt = fr.packet
                self.packet_count += 1

                # Get portnum
                if pkt.HasField('decoded'):
                    portnum = pkt.decoded.portnum
                    payload = pkt.decoded.payload

                    from_id = f"!{pkt.from_:08x}" if pkt.from_ else "?"
                    to_id = f"!{pkt.to:08x}" if pkt.to else "?"

                    if portnum == portnums_pb2.POSITION_APP:
                        pos = self.decode_position(payload)
                        if pos and pos['latitude']:
                            print(f"[{ts}] POSITION from {from_id}:")
                            print(f"  Lat: {pos['latitude']:.7f}  Lon: {pos['longitude']:.7f}")
                            if pos['altitude']:
                                print(f"  Alt: {pos['altitude']}m")
                            if pos['speed']:
                                speed_kts = pos['speed'] * 1.94384
                                print(f"  Speed: {speed_kts:.1f} kts ({pos['speed']:.1f} m/s)")
                            if pos['heading']:
                                print(f"  Heading: {pos['heading']:.1f}°")
                            if pos['sats']:
                                print(f"  Sats: {pos['sats']}  PDOP: {pos['pdop']}")
                            print()

                    elif portnum == portnums_pb2.TELEMETRY_APP:
                        tel = self.decode_telemetry(payload)
                        if tel and tel.get('battery') is not None:
                            print(f"[{ts}] TELEMETRY from {from_id}: Battery {tel['battery']}% ({tel.get('voltage', 0):.2f}V)")

                    elif portnum == portnums_pb2.NODEINFO_APP:
                        try:
                            user = mesh_pb2.User()
                            user.ParseFromString(payload)
                            print(f"[{ts}] NODEINFO from {from_id}: {user.long_name} ({user.short_name})")
                        except:
                            pass

                    else:
                        # Other packet types
                        portname = portnums_pb2.PortNum.Name(portnum)
                        print(f"[{ts}] {portname} from {from_id} ({len(payload)} bytes)")

            elif fr.HasField('my_info'):
                info = fr.my_info
                print(f"[{ts}] MY_INFO: node_num={info.my_node_num}")

            elif fr.HasField('node_info'):
                ni = fr.node_info
                if ni.HasField('user'):
                    print(f"[{ts}] NODE_INFO: {ni.user.long_name} ({ni.user.short_name}) - {ni.user.hw_model}")

            elif fr.HasField('config_complete_id'):
                print(f"[{ts}] Config complete")

        except Exception as e:
            print(f"[{ts}] Decode error: {e}")
            print(f"  Raw: {data.hex()[:100]}...")

    def handle_notify(self, sender, data):
        """Handle notification from FromNum characteristic."""
        # FromNum notifications just signal new data is available
        # We need to read from FromRadio to get the actual data
        pass

    async def read_radio_loop(self):
        """Continuously read from FromRadio characteristic."""
        from bleak import BleakClient

        while True:
            try:
                data = await self.client.read_gatt_char(FROMRADIO_UUID)
                if data and len(data) > 0:
                    self.handle_fromradio(data)
                else:
                    # No more data, wait a bit
                    await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Read error: {e}")
                await asyncio.sleep(1)

    async def run(self):
        """Connect and monitor."""
        from bleak import BleakClient, BleakScanner

        # Scan if we have a name, not an address
        address = self.device_address
        if not ':' in address:
            print(f"Scanning for device '{address}'...")
            devices = await BleakScanner.discover(timeout=10)
            for d in devices:
                if d.name == address:
                    address = d.address
                    print(f"Found: {d.name} at {d.address}")
                    break
            else:
                print(f"Device '{self.device_address}' not found")
                return

        print(f"Connecting to {address}...")

        async with BleakClient(address) as client:
            self.client = client
            print("Connected!")

            # Check services
            for service in client.services:
                if SERVICE_UUID.lower() in service.uuid.lower():
                    print(f"Found Meshtastic service")
                    break
            else:
                print("Warning: Meshtastic service not found")

            # Try to start notifications on FromNum (signals new data)
            try:
                await client.start_notify(FROMNUM_UUID, self.handle_notify)
                print("Subscribed to FromNum notifications")
            except Exception as e:
                print(f"Could not subscribe to FromNum: {e}")

            print("\nListening for packets... (Ctrl+C to exit)\n")

            # Read loop
            try:
                await self.read_radio_loop()
            except asyncio.CancelledError:
                pass

            print(f"\nReceived {self.packet_count} packets")


async def scan_devices():
    """Scan for Meshtastic devices."""
    from bleak import BleakScanner

    print("Scanning for Meshtastic BLE devices...")
    devices = await BleakScanner.discover(timeout=10)

    found = []
    for d in devices:
        if d.name and ('mesh' in d.name.lower() or d.name.startswith('AT') or 'Meshtastic' in d.name):
            found.append(d)
            print(f"  Found: {d.name} - {d.address}")

    if not found:
        print("  No Meshtastic devices found")

    return found


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Monitor Meshtastic node via raw BLE')
    parser.add_argument('device', nargs='?', help='Device name or address')
    parser.add_argument('--scan', action='store_true', help='Just scan for devices')
    args = parser.parse_args()

    if args.scan:
        asyncio.run(scan_devices())
        return 0

    if not args.device:
        print("Usage: ble_raw_monitor.py <device_name_or_address>")
        print("       ble_raw_monitor.py --scan")
        return 1

    monitor = RawBLEMonitor(args.device)
    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        print("\nExiting...")

    return 0


if __name__ == '__main__':
    sys.exit(main())
