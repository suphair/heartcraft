#!/bin/bash
# Выкладка сетевого Сердцекрафта на роутер Keenetic (Entware в /opt/heartcraft).
#
#   ./router/deploy.sh
#
# Транспорт как у судоку и ds-бота: tar-стрим через ssh (scp/rsync на роутере нет),
# пароль SSH-root даёт SSH_ASKPASS из ~/.config/keenetic/askpass.sh.
# Мир (world.json) и пароль комнаты (password) живут только на роутере и выкладкой
# не затираются; пароля нет - выкладка заводит случайный и печатает его.
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

echo "заливаю страницу и сервер в /opt/heartcraft..."
rssh "mkdir -p /opt/heartcraft"
COPYFILE_DISABLE=1 tar czf - index.html -C router server.py keeper.sh | rssh "cd /opt/heartcraft && tar xzf - && chmod +x keeper.sh && sh -n keeper.sh"
COPYFILE_DISABLE=1 tar czf - -C router S99heartcraft | rssh "cd /opt/etc/init.d && tar xzf - && chmod +x S99heartcraft"

NEW_PASS="$(python3 -c 'import secrets; print(secrets.randbelow(900000) + 100000)')"
rssh "cd /opt/heartcraft && if [ ! -s password ]; then echo '$NEW_PASS' > password; chmod 600 password; echo \"заведён пароль комнаты: \$(cat password)\"; fi"

echo "перезапуск и проверка..."
rssh "/opt/etc/init.d/S99heartcraft restart
      for i in \$(seq 1 30); do
        sleep 2
        wget -qO- http://127.0.0.1:8092/ 2>/dev/null | grep '<title>Сердцекрафт' >/dev/null && { echo 'страница отвечает'; exit 0; }
      done
      echo 'ПУСТО: сервер не ответил'; tail -20 /opt/heartcraft/server.log; exit 1"
echo "деплой завершён."
