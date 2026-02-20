#!/usr/bin/env python3
"""Send a command to a GT06 tracker via the server admin API."""

import sys
import argparse
import json
from urllib.request import Request, urlopen
from urllib.parse import quote
from urllib.error import URLError, HTTPError


def main():
    parser = argparse.ArgumentParser(description="Send a command to a GT06 tracker")
    parser.add_argument("user_id", help="GT06 device user ID (e.g. G226122)")
    parser.add_argument("command", help="Command to send (e.g. FIND#)")
    parser.add_argument("-H", "--host", default="wstracker.org",
                        help="Server hostname (default: wstracker.org)")
    parser.add_argument("-p", "--port", type=int, default=41234,
                        help="Server port (default: 41234)")
    parser.add_argument("-e", "--event", type=int, default=1,
                        help="Event ID (default: 1)")
    parser.add_argument("-P", "--password", default=None,
                        help="Admin password (default: prompt)")
    args = parser.parse_args()

    password = args.password
    if password is None:
        import getpass
        password = getpass.getpass("Admin password: ")

    url = (f"http://{args.host}:{args.port}/api/event/{args.event}"
           f"/admin/gt06-cmd/{quote(args.user_id)}?cmd={quote(args.command)}")

    req = Request(url)
    req.add_header("X-Admin-Password", password)

    try:
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if data.get("success"):
            print(f"Sent '{args.command}' to {args.user_id}")
        else:
            print(f"Response: {json.dumps(data)}")
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            data = json.loads(body)
            print(f"Error: {data.get('error', body)}", file=sys.stderr)
        except Exception:
            print(f"HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
