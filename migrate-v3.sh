#!/bin/bash
# Однократный переход установленной ноды с правил v1/v2 на каскад v3.
# Запуск из этого дерева: sudo bash migrate-v3.sh
# Не меняет subscription.url, config.base.yaml, local-rules.yaml и ядро.
# При ошибке возвращает прежние файлы. Порт проверки: PROXY=127.0.0.1:7890.
set -u
SRC=$(cd "$(dirname "$0")" && pwd)
E=${MIHOMO_BASE:-/etc/mihomo}
PROXY=${PROXY:-127.0.0.1:7890}
B=/root/mihomo-v3-bak-$(date +%Y%m%d-%H%M%S)

die() { echo "ОШИБКА: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "нужен root"
[ -s "$E/routing-rules.url" ] || die "нет $E/routing-rules.url - сначала обычная установка"
[ -f "$SRC/usr/local/sbin/mihomo-build-config" ] || die "запускайте из полного дерева репозитория"
if [ "$(cat "$E/cascade-contract" 2>/dev/null)" = 3 ]; then echo "уже v3"; exit 0; fi

mkdir -p "$B" && cp -a "$E" "$B/etc" && cp -p /usr/local/sbin/mihomo-build-config "$B/" || die "бэкап не удался"
echo "бэкап: $B"

restore() {
  echo "ОШИБКА: $1, откатываю" >&2
  rm -f "$E/cascade-contract"
  cp -p "$B/etc/routing-rules.url" "$E/routing-rules.url"
  [ -f "$B/etc/alias-labels.json" ] && cp -p "$B/etc/alias-labels.json" "$E/"
  install -m 755 "$B/mihomo-build-config" /usr/local/sbin/mihomo-build-config
  systemctl restart mihomo; sleep 3
  echo "mihomo: $(systemctl is-active mihomo); бэкап: $B" >&2
  exit 1
}

install -m 755 "$SRC/usr/local/sbin/mihomo-build-config" /usr/local/sbin/mihomo-build-config
echo 3 > "$E/cascade-contract"
sed -i -E 's#/rules(-v2)?\.yaml#/rules-v3.yaml#' "$E/routing-rules.url"
rm -f "$E/alias-labels.json"   # v3 не зависит от имен нод

systemctl restart mihomo; sleep 5
[ "$(systemctl is-active mihomo)" = active ] || { journalctl -u mihomo -n 15 --no-pager >&2; restore "mihomo не запустился"; }
grep -q '"contract": 3' "$E/build-state.json" 2>/dev/null || restore "сборка не v3"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -x "socks5h://$PROXY" https://github.com)
[ "$code" = 200 ] || restore "github через $PROXY ответил $code"

if ( cd "$SRC" && bash install.sh --step updater ) && mihomo-update enable --accept-current; then
  echo "автообновление сборщика включено"
else
  echo "автообновление не включилось; v3 работает, включить: docs/autoupdate.md"
fi
echo "ГОТОВО: v3. бэкап: $B"
