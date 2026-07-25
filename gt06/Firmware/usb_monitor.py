#!/usr/bin/env python3
"""
usb_monitor.py - passively log every USB serial port the W07C exposes, with timestamps.
Used to capture on-device FOTA / debug output during an OTA test.

  python3 usb_monitor.py [--secs N] [--baud 115200] [--ports /dev/ttyUSB0,...]

Writes gt06/Firmware/usb_<portname>.log, one timestamped line per received chunk
(both hex and printable-ASCII rendering). Runs until --secs elapses or Ctrl-C.
"""
import sys, os, time, threading, argparse, glob

try:
    import serial
except ImportError:
    sys.exit("pyserial required: pip install pyserial")

HERE = os.path.dirname(os.path.abspath(__file__))


def render(b):
    return "".join(chr(x) if 32 <= x < 127 else "." for x in b)


def log_port(dev, baud, stop_evt, t0):
    name = os.path.basename(dev)
    path = os.path.join(HERE, "usb_%s.log" % name)
    try:
        ser = serial.Serial(dev, baud, timeout=0.2)
    except Exception as e:
        with open(path, "a") as f:
            f.write("[%.3f] OPEN FAILED: %s\n" % (time.time() - t0, e))
        return
    with open(path, "a", buffering=1) as f:
        f.write("==== open %s @ %d  (t0=%.3f) ====\n" % (dev, baud, t0))
        buf = b""
        while not stop_evt.is_set():
            try:
                data = ser.read(4096)
            except Exception as e:
                f.write("[%.3f] READ ERR: %s\n" % (time.time() - t0, e))
                break
            if data:
                f.write("[%9.3f] %-6s %s | %s\n"
                        % (time.time() - t0, name, data.hex(), render(data)))
        ser.close()
        f.write("==== closed %s ====\n" % dev)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=0, help="0 = run until Ctrl-C")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--ports", default="")
    a = ap.parse_args()
    ports = a.ports.split(",") if a.ports else sorted(glob.glob("/dev/ttyUSB*"))
    if not ports:
        sys.exit("no /dev/ttyUSB* ports found")
    t0 = time.time()
    stop = threading.Event()
    threads = [threading.Thread(target=log_port, args=(p, a.baud, stop, t0), daemon=True)
               for p in ports]
    for t in threads:
        t.start()
    print("logging %s -> gt06/Firmware/usb_*.log  (%s)"
          % (ports, "%.0fs" % a.secs if a.secs else "until Ctrl-C"))
    try:
        if a.secs:
            time.sleep(a.secs)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop.set()
    for t in threads:
        t.join(timeout=2)
    print("done")


if __name__ == "__main__":
    main()
