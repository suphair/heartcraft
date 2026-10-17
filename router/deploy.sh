#!/bin/bash
# Выкладка сетевого Сердцекрафта на роутер Keenetic (Entware в /opt/heartcraft).
#
#   ./router/deploy.sh
#
# Сервер игры работает на Windows-машине: она запускает host.cmd с SMB-шары роутера, берёт оттуда код,
# а миры пишет туда же. Роутер только пробрасывает 192.168.1.1:8092 на эту машину (hub.sh + iptables).
# Транспорт как у судоку и ds-бота: tar-стрим через ssh (scp/rsync на роутере нет),
# пароль SSH-root даёт SSH_ASKPASS из ~/.config/keenetic/askpass.sh.
# Список миров (worlds), админ (admin), данные миров (data/) и хозяин (host) живут только на роутере и
# выкладкой не затираются. Без worlds или admin сервер не стартует - выкладка останавливается и
# говорит, что завести: admin держит логин и код админа, в репозиторий им нельзя.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
HOST=192.168.1.1
PORT=222
ASK="$HOME/.config/keenetic/askpass.sh"

rssh() { SSH_ASKPASS="$ASK" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
  ssh -p "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o LogLevel=ERROR -o ConnectTimeout=30 -o NumberOfPasswordPrompts=1 \
      root@"$HOST" "$@"; }

cd "$HERE"
python3 -m py_compile router/server.py

rssh "command -v iptables >/dev/null" || { echo "на роутере нет iptables: opkg install iptables"; exit 1; }

echo "заливаю страницу, сервер и проброс в /opt/heartcraft..."
rssh "mkdir -p /opt/heartcraft /opt/etc/ndm/netfilter.d"
COPYFILE_DISABLE=1 tar czf - index.html -C router server.py host.cmd hub.sh | rssh "cd /opt/heartcraft && tar xzf - && chmod +x hub.sh && sh -n hub.sh"
COPYFILE_DISABLE=1 tar czf - -C router S99heartcraft | rssh "cd /opt/etc/init.d && tar xzf - && chmod +x S99heartcraft"
COPYFILE_DISABLE=1 tar czf - -C router 100-heartcraft.sh | rssh "cd /opt/etc/ndm/netfilter.d && tar xzf - && chmod +x 100-heartcraft.sh"

rssh "cd /opt/heartcraft && for f in worlds admin; do [ -s \$f ] || { echo \"нет /opt/heartcraft/\$f: worlds - строки «код название», admin - «логин код»\"; exit 1; }; done"

echo "перезапускаю проброс..."
rssh "/opt/etc/init.d/S99heartcraft restart
      sleep 2
      pgrep -f '[/]opt/heartcraft/hub.sh loop' >/dev/null || { echo 'ПУСТО: hub.sh не запустился'; exit 1; }
      iptables -t nat -S HEARTCRAFT_PRE
      [ -s /opt/heartcraft/host ] || { echo 'хозяина пока нет: запусти host.cmd на Windows'; exit 0; }
      ip=\$(cut -d' ' -f1 /opt/heartcraft/host)
      python3 -c \"import urllib.request; urllib.request.urlopen('http://\$ip:8092/', timeout=5)\" 2>/dev/null \
        && echo \"сервер на \$ip отвечает\" || echo \"сервер на \$ip не отвечает: запущен ли host.cmd?\""
echo "деплой завершён."
