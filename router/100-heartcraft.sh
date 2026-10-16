#!/bin/sh
# Keenetic netfilter hook (/opt/etc/ndm/netfilter.d): a rebuilt table loses the game forwarding.
[ "$type" = "iptables" ] || exit 0
[ "$table" = "nat" ] || [ "$table" = "filter" ] || exit 0
pgrep -f "[/]opt/heartcraft/hub.sh loop" >/dev/null 2>&1 || exit 0
/opt/heartcraft/hub.sh apply
