#!/bin/bash
# Record a game night (stage 0). Read-only: no account, no keys, no orders.
#
#   bash scripts/record_night.sh         -> records every NBA game from 3h before tip-off to 30 min after the end
#   bash scripts/record_night.sh shadow  -> same, plus stage 2 (live detector + hypothetical orders, logged only)
#
# `caffeinate -is` keeps the Mac from sleeping while it runs. Closing the lid still sleeps a MacBook, so leave the
# lid open (and plug in the charger for a long night). A sleeping Mac drops the connection and you lose that part.
# It stops by itself 15 minutes after the last game of the night is over. Ctrl-C also stops it cleanly.
cd "$(dirname "$0")/.." || exit 1
MODE="${1:-record}"
echo "Starting '$MODE' at $(date). Output: $(pwd)/data   (Ctrl-C to stop)"
exec caffeinate -is python3 -m polyarb "$MODE" --out data --auto-stop-min 15
