#!/usr/bin/env bash
# Деплой приложения на сервер: новая папка релиза, переключение симлинка current, перезапуск liv-web,
# проверка здоровья и автоматический откат при ошибке.
#
# Настройки — в .env.deploy рядом со скриптом (образец: .env.deploy.example) или в переменных окружения:
#   LIV_HOST, LIV_USER — сервер и пользователь с sudo (обязательно)
#   LIV_PORT           — порт ssh (по умолчанию 22)
#   LIV_PASS           — пароль для ssh и sudo (нужен sshpass); без него ssh и sudo спросят сами
#   LIV_KEEP           — сколько релизов хранить (по умолчанию 5)
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
if [[ -f "$HERE/.env.deploy" ]]; then
  set -a; source "$HERE/.env.deploy"; set +a
fi
HOST=${LIV_HOST:?Укажи LIV_HOST в .env.deploy (см. .env.deploy.example)}
USER_=${LIV_USER:?Укажи LIV_USER в .env.deploy (см. .env.deploy.example)}
PORT=${LIV_PORT:-22}
KEEP=${LIV_KEEP:-5}
UPLOAD=liv-upload

for f in serve.py ingredients.py public/index.html nginx/liv.conf systemd/liv-web.service; do
  [[ -f "$HERE/$f" ]] || { echo "Нет файла $f" >&2; exit 1; }
done
python3 -m py_compile "$HERE/serve.py" "$HERE/ingredients.py"

SSH_OPTS=(-p "$PORT" -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR)
if [[ -n "${LIV_PASS:-}" ]]; then
  command -v sshpass >/dev/null || { echo "Для LIV_PASS нужен sshpass" >&2; exit 1; }
  export SSHPASS=$LIV_PASS
  WRAP=(sshpass -e)
else
  WRAP=()
fi
remote() { "${WRAP[@]}" ssh "${SSH_OPTS[@]}" "$USER_@$HOST" "$@"; }

echo "→ загрузка файлов на $USER_@$HOST"
"${WRAP[@]}" rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
  "$HERE/serve.py" "$HERE/ingredients.py" "$HERE/public" "$HERE/nginx" "$HERE/systemd" "$USER_@$HOST:$UPLOAD/"

REMOTE_SCRIPT=$(cat <<EOF
set -euo pipefail
SRC=\$(getent passwd "$USER_" | cut -d: -f6)/$UPLOAD
BASE=/srv/liv-web
NEW=\$BASE/releases/release.\$(date +%Y%m%d-%H%M%S)
PREV=\$(readlink -f \$BASE/current || true)
install -d -m 755 \$BASE/releases "\$NEW"
cp -r "\$SRC"/serve.py "\$SRC"/ingredients.py "\$SRC"/public "\$NEW"/
chown -R root:root "\$NEW"; chmod -R u=rwX,go=rX "\$NEW"
ln -sfn "\$NEW" \$BASE/current.tmp && mv -T \$BASE/current.tmp \$BASE/current
# systemd-юнит (User= берём из LIV_USER): ставим при первом деплое и обновляем, если поменялся
sed "s/^User=.*/User=$USER_/" "\$SRC/systemd/liv-web.service" > "\$SRC/liv-web.service.rendered"
if ! cmp -s "\$SRC/liv-web.service.rendered" /etc/systemd/system/liv-web.service; then
  install -m 644 "\$SRC/liv-web.service.rendered" /etc/systemd/system/liv-web.service
  systemctl daemon-reload && systemctl enable -q liv-web && echo "✓ systemd-юнит обновлён"
fi
systemctl restart liv-web
ok=
for i in \$(seq 1 20); do
  if curl -fsS -m 3 http://127.0.0.1:8080/api/recipe-state >/dev/null 2>&1; then ok=1; break; fi
  sleep 0.5
done
if [[ -z "\$ok" ]]; then
  echo "✗ сервис не поднялся, откат на \$PREV" >&2
  journalctl -u liv-web -n 30 --no-pager >&2 || true
  if [[ -n "\$PREV" ]]; then ln -sfn "\$PREV" \$BASE/current.tmp && mv -T \$BASE/current.tmp \$BASE/current; systemctl restart liv-web; fi
  exit 1
fi
# nginx: наружу на 80 порт (ставим при первом деплое, конфиг обновляем при изменениях)
if ! command -v nginx >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q nginx >/dev/null
fi
if ! cmp -s "\$SRC/nginx/liv.conf" /etc/nginx/sites-available/liv.conf; then
  install -m 644 "\$SRC/nginx/liv.conf" /etc/nginx/sites-available/liv.conf
  ln -sfn /etc/nginx/sites-available/liv.conf /etc/nginx/sites-enabled/liv.conf
  rm -f /etc/nginx/sites-enabled/default
  nginx -t -q && systemctl reload-or-restart nginx && echo "✓ nginx обновлён"
fi
systemctl enable -q nginx
curl -fsS -m 5 -o /dev/null http://127.0.0.1/api/recipe-state || { echo "✗ nginx не отвечает" >&2; exit 1; }
# Храним последние $KEEP релизов (текущий не трогаем)
ls -1dt \$BASE/releases/release.* | tail -n +$((KEEP + 1)) | grep -vx "\$NEW" | xargs -r rm -rf
echo "✓ работает: \$NEW"
EOF
)

echo "→ установка релиза и перезапуск"
if [[ -n "${LIV_PASS:-}" ]]; then
  # первая строка stdin — пароль для sudo, остальное — скрипт для bash
  printf '%s\n%s\n' "$LIV_PASS" "$REMOTE_SCRIPT" | remote "sudo -k -S -p '' bash -s"
else
  ssh -t "${SSH_OPTS[@]}" "$USER_@$HOST" "sudo bash -c $(printf '%q' "$REMOTE_SCRIPT")"
fi
