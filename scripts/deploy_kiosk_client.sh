#!/bin/bash
# Update the Nina kiosk client on the kitchen kiosk: fetch the repo there, check out a ref, restart the service.
#   scripts/deploy_kiosk_client.sh [ssh-host=kiosk] [ref=origin/main]
set -u
HOST=${1:-kiosk}; REF=${2:-origin/main}
ssh "$HOST" "set -e; cd ~/nina-kiosk/src && git fetch -q origin && git checkout -q --detach $REF && git log --oneline -1 && systemctl --user restart nina-kiosk.service && sleep 4 && systemctl --user is-active nina-kiosk.service && journalctl --user -u nina-kiosk.service --since '-10s' --no-pager | tail -3" || { echo "deploy failed"; exit 1; }
