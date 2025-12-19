#!/bin/bash
#
# Migration script for multi-event support
# Run this on the production server to migrate from single-event to multi-event mode
#
# This script:
# 1. Creates events.json with event 1 (NZ Interdominion 2026)
# 2. Creates the html/1/ directory structure
# 3. Moves existing data into html/1/
#
# IMPORTANT: Stop the tracker server before running this script!
#

set -e  # Exit on any error

TRACKER_DIR="${TRACKER_DIR:-$HOME/tracker}"
cd "$TRACKER_DIR"

echo "=== Windsurfer Tracker Multi-Event Migration ==="
echo "Working directory: $TRACKER_DIR"
echo ""

# Check if main server is running (not beta)
if pgrep -f "tracker_server.py --manager-password" > /dev/null || pgrep -f "tracker_server.py --admin-password" > /dev/null; then
    echo "ERROR: tracker_server.py is still running!"
    echo "Stop the server first (kill the screen session or Ctrl+C)"
    exit 1
fi

# Check if already migrated
if [ -f "events.json" ]; then
    echo "events.json already exists - migration may have already been done"
    echo "Contents:"
    cat events.json
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Create events.json with event 1
echo "Creating events.json..."
cat > events.json << 'EOF'
{
  "next_eid": 2,
  "events": {
    "1": {
      "name": "NZ Interdominion 2026",
      "description": "Wellington Harbour - January 2026",
      "admin_password": "NZInterdominionAdmin",
      "tracker_password": "NZInterdominion",
      "archived": false,
      "created": 1734567600,
      "created_iso": "2024-12-19T12:00:00"
    }
  }
}
EOF
echo "  Created events.json"

# Create event 1 directory
echo "Creating html/1/ directory structure..."
mkdir -p html/1/logs

# Move existing data into event 1 directory
echo "Moving existing data to html/1/..."

# Move logs directory contents
if [ -d "html/logs" ] && [ "$(ls -A html/logs 2>/dev/null)" ]; then
    echo "  Moving logs..."
    mv html/logs/* html/1/logs/ 2>/dev/null || true
    rmdir html/logs 2>/dev/null || true
elif [ -d "logs" ] && [ "$(ls -A logs 2>/dev/null)" ]; then
    echo "  Moving logs from root..."
    mv logs/* html/1/logs/ 2>/dev/null || true
fi

# Move current_positions.json
if [ -f "html/current_positions.json" ]; then
    echo "  Moving current_positions.json..."
    mv html/current_positions.json html/1/
elif [ -f "current_positions.json" ]; then
    echo "  Moving current_positions.json from root..."
    mv current_positions.json html/1/
fi

# Move course.json
if [ -f "html/course.json" ]; then
    echo "  Moving course.json..."
    mv html/course.json html/1/
elif [ -f "course.json" ]; then
    echo "  Moving course.json from root..."
    mv course.json html/1/
fi

# Move users.json
if [ -f "html/users.json" ]; then
    echo "  Moving users.json..."
    mv html/users.json html/1/
elif [ -f "users.json" ]; then
    echo "  Moving users.json from root..."
    mv users.json html/1/
fi

echo ""
echo "=== Migration Complete ==="
echo ""
echo "New directory structure:"
echo "  $TRACKER_DIR/"
echo "  ├── events.json          (event configuration)"
echo "  ├── tracker_server.py"
echo "  └── html/"
echo "      ├── index.html       (event picker)"
echo "      ├── event.html       (live tracking)"
echo "      ├── manage.html      (event management)"
echo "      └── 1/               (event 1 data)"
echo "          ├── logs/"
echo "          ├── current_positions.json"
echo "          ├── course.json"
echo "          └── users.json"
echo ""
echo "Start the server with:"
echo "  sudo systemctl start tracker"
echo ""
echo "Or manually:"
echo "  python3 tracker_server.py --manager-password=NZInterdominionManager --events-file=events.json --static-dir=html"
echo ""
echo "Manager password: NZInterdominionManager"
echo "Event management: https://wstracker.org/manage.html"
