#!/bin/bash
# Windsurfer Tracker Server - Multi-Event Mode
#
# Event management: https://wstracker.org/manage.html
# Manager password is for creating/editing events
# Each event has its own admin and tracker passwords set via manage.html
#
cd $HOME/tracker
python3 tracker_server.py \
    --manager-password=NZInterdominionManager \
    --events-file=events.json \
    --static-dir=html





