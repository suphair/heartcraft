#!/bin/sh
# The game server runs on a Windows machine; the router only forwards 192.168.1.1:8092 to it
# and points the KeenDNS proxy (https://heartcraft.<name>.keenetic.link) at it.
# host.cmd there writes "<ip> <token>" into /opt/heartcraft/host over SMB.
#   hub.sh loop  - keeps the forwarding and the proxy in step with that file (started by S99heartcraft)
#   hub.sh apply - sets the rules once: Keenetic drops foreign rules when it rebuilds its tables
#   hub.sh clear - removes the forwarding
PATH=/opt/bin:/opt/sbin:/opt/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
HOST_FILE=/opt/heartcraft/host
LAN=br0
ROUTER=192.168.1.1
NET=192.168.1.0/24
PORT=8092
PROXY=heartcraft

target() {
    ip=$(cut -d' ' -f1 "$HOST_FILE" 2>/dev/null)
    echo "$ip" | grep -Eq '^192\.168\.1\.[0-9]{1,3}$' && echo "$ip"
}

chain() {
    iptables -t "$1" -N "$3" 2>/dev/null
    iptables -t "$1" -F "$3"
    iptables -t "$1" -C "$2" -j "$3" 2>/dev/null || iptables -t "$1" -I "$2" -j "$3"
}

apply() {
    chain nat PREROUTING HEARTCRAFT_PRE
    chain nat POSTROUTING HEARTCRAFT_POST
    chain filter FORWARD HEARTCRAFT_FWD
    [ -n "$1" ] || return 0
    iptables -t nat -A HEARTCRAFT_PRE -i $LAN -d $ROUTER -p tcp --dport $PORT -j DNAT --to-destination "$1:$PORT"
    iptables -t nat -A HEARTCRAFT_POST -o $LAN -s $NET -d "$1" -p tcp --dport $PORT -j MASQUERADE
    iptables -t filter -A HEARTCRAFT_FWD -i $LAN -o $LAN -d "$1" -p tcp --dport $PORT -j ACCEPT
}

case "$1" in
    apply)
        apply "$(target)"
        ;;
    clear)
        apply ""
        ;;
    loop)
        last=none
        while true; do
            ip=$(target)
            if [ "$ip" != "$last" ]; then
                apply "$ip" && ndmc -c "ip http proxy $PROXY upstream http ${ip:-127.0.0.1} $PORT" >/dev/null && last=$ip
                echo "$(date '+%F %T') forwarding :$PORT -> ${ip:-nobody}"
            fi
            sleep 5
        done
        ;;
    *)
        echo "usage: $0 loop|apply|clear" >&2
        exit 1
        ;;
esac
