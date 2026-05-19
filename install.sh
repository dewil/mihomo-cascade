#!/bin/bash
set -euo pipefail

MIHOMO_VERSION="v1.19.25"
GITHUB_REPO="${GITHUB_REPO:-dewil/mihomo-cascade}"
GITHUB_REF="${GITHUB_REF:-main}"
INSTALL_SUBDIR="${INSTALL_SUBDIR:-}"
ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  ARCH_DL="amd64" ;;
  aarch64) ARCH_DL="arm64" ;;
  *)       echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP_FETCH=""

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo)."
  exit 1
fi

# Режим установки: fresh (с нуля) или update (на уже развёрнутый mihomo-cascade).
# Детект автоматический: если /etc/mihomo/subscription.url уже непустой — это update.
# Можно форсировать через MIHOMO_INSTALL_MODE=fresh|update.
MODE="${MIHOMO_INSTALL_MODE:-}"
if [ -z "$MODE" ]; then
  if [ -s /etc/mihomo/subscription.url ]; then
    MODE="update"
  else
    MODE="fresh"
  fi
fi
case "$MODE" in
  fresh|update) ;;
  *) echo "Unknown MIHOMO_INSTALL_MODE: $MODE (expected fresh|update)"; exit 1 ;;
esac
echo "=== Режим установки: ${MODE} ==="

NOLOGIN_BIN="$(command -v nologin || true)"
if [ -z "$NOLOGIN_BIN" ]; then
  if [ -x /usr/sbin/nologin ]; then
    NOLOGIN_BIN="/usr/sbin/nologin"
  elif [ -x /sbin/nologin ]; then
    NOLOGIN_BIN="/sbin/nologin"
  else
    echo "nologin binary not found."
    exit 1
  fi
fi

cleanup() {
  [ -n "$TMP_FETCH" ] && rm -rf "$TMP_FETCH"
}
trap cleanup EXIT

build_raw_base_url() {
  local base="https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_REF}"
  if [ -n "${INSTALL_SUBDIR}" ] && [ "${INSTALL_SUBDIR}" != "." ]; then
    base="${base}/${INSTALL_SUBDIR}"
  fi
  echo "${base}"
}

fetch_from_github() {
  local rel="$1"
  local dst="$2"
  local base
  base="$(build_raw_base_url)"
  curl -fsSL "${base}/${rel}" -o "${dst}"
  if [ ! -s "${dst}" ]; then
    echo "Downloaded file is empty: ${rel}" >&2
    exit 1
  fi
}

prepare_source_dir() {
  if [ -d "${SCRIPT_DIR}/etc/mihomo" ] && [ -d "${SCRIPT_DIR}/usr/local/sbin" ]; then
    echo "  -> using local files from ${SCRIPT_DIR}" >&2
    echo "${SCRIPT_DIR}"
    return
  fi
  TMP_FETCH="$(mktemp -d)"
  mkdir -p "${TMP_FETCH}/etc/mihomo" "${TMP_FETCH}/etc/systemd/system" "${TMP_FETCH}/etc/cron.d" "${TMP_FETCH}/usr/local/sbin"

  fetch_from_github "etc/mihomo/config.base.yaml" "${TMP_FETCH}/etc/mihomo/config.base.yaml"
  fetch_from_github "etc/mihomo/subscription.url" "${TMP_FETCH}/etc/mihomo/subscription.url"
  fetch_from_github "etc/mihomo/routing-rules.url" "${TMP_FETCH}/etc/mihomo/routing-rules.url"
  fetch_from_github "etc/mihomo/routing-rules.yaml" "${TMP_FETCH}/etc/mihomo/routing-rules.yaml"
  fetch_from_github "etc/mihomo/iso3166_alpha2.txt" "${TMP_FETCH}/etc/mihomo/iso3166_alpha2.txt"
  fetch_from_github "etc/mihomo/local-rules.yaml" "${TMP_FETCH}/etc/mihomo/local-rules.yaml"
  fetch_from_github "etc/systemd/system/mihomo.service" "${TMP_FETCH}/etc/systemd/system/mihomo.service"
  fetch_from_github "etc/cron.d/mihomo-refresh" "${TMP_FETCH}/etc/cron.d/mihomo-refresh"
  fetch_from_github "usr/local/sbin/mihomo-build-config" "${TMP_FETCH}/usr/local/sbin/mihomo-build-config"
  fetch_from_github "usr/local/sbin/mihomo-refresh" "${TMP_FETCH}/usr/local/sbin/mihomo-refresh"
  fetch_from_github "usr/local/sbin/check-route" "${TMP_FETCH}/usr/local/sbin/check-route"

  chmod 755 "${TMP_FETCH}/usr/local/sbin/mihomo-build-config" "${TMP_FETCH}/usr/local/sbin/mihomo-refresh" "${TMP_FETCH}/usr/local/sbin/check-route"
  local source_suffix="/"
  if [ -n "${INSTALL_SUBDIR}" ] && [ "${INSTALL_SUBDIR}" != "." ]; then
    source_suffix="/${INSTALL_SUBDIR}/"
  fi
  echo "  -> files fetched from GitHub: ${GITHUB_REPO}@${GITHUB_REF}${source_suffix}" >&2
  echo "${TMP_FETCH}"
}

SOURCE_DIR="$(prepare_source_dir)"

echo "=== 1. Скачиваем mihomo ${MIHOMO_VERSION} (${ARCH_DL}) ==="
INSTALLED_VERSION=""
if [ -x /usr/local/bin/mihomo ]; then
  INSTALLED_VERSION="$(/usr/local/bin/mihomo -v 2>/dev/null | head -1 | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
fi
if [ "$INSTALLED_VERSION" = "$MIHOMO_VERSION" ]; then
  echo "  -> /usr/local/bin/mihomo уже ${MIHOMO_VERSION}, пропускаем"
else
  TMP=$(mktemp -d)
  curl -fsSL "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-${ARCH_DL}-${MIHOMO_VERSION}.gz" \
    -o "${TMP}/mihomo.gz"
  gunzip "${TMP}/mihomo.gz"
  install -m 755 "${TMP}/mihomo" /usr/local/bin/mihomo
  rm -rf "$TMP"
  echo "  -> /usr/local/bin/mihomo установлен (${INSTALLED_VERSION:-нет} -> ${MIHOMO_VERSION})"
fi

echo "=== 2. Создаём пользователя mihomo ==="
if ! id mihomo &>/dev/null; then
  useradd -r -s "$NOLOGIN_BIN" -d /etc/mihomo mihomo
  echo "  -> пользователь mihomo создан"
else
  echo "  -> пользователь mihomo уже существует"
fi

echo "=== 3. Копируем конфигурацию ==="
install -d -o root -g mihomo -m 750 /etc/mihomo
install -d -o root -g mihomo -m 750 /etc/mihomo/providers

# Файлы из репо (источник правды) — затираем всегда:
for f in config.base.yaml iso3166_alpha2.txt local-rules.yaml; do
  install -o root -g mihomo -m 640 "${SOURCE_DIR}/etc/mihomo/${f}" "/etc/mihomo/${f}"
done

# Пользовательские файлы — кладём только в fresh-режиме, иначе оставляем как есть:
if [ "$MODE" = "fresh" ]; then
  for f in subscription.url routing-rules.url routing-rules.yaml; do
    install -o root -g mihomo -m 640 "${SOURCE_DIR}/etc/mihomo/${f}" "/etc/mihomo/${f}"
  done
  echo "  -> конфиги скопированы в /etc/mihomo/"
else
  echo "  -> repo-managed файлы обновлены, пользовательские (subscription.url, routing-rules.*) сохранены"
fi

echo "=== 4. Скачиваем GeoIP базу ==="
if [ ! -f /etc/mihomo/geoip.metadb ]; then
  curl -fsSL "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.metadb" \
    -o /etc/mihomo/geoip.metadb
  echo "  -> geoip.metadb скачан"
else
  echo "  -> geoip.metadb уже есть"
fi

echo "=== 5. Устанавливаем скрипты ==="
install -m 755 "${SOURCE_DIR}/usr/local/sbin/mihomo-build-config" /usr/local/sbin/mihomo-build-config
install -m 755 "${SOURCE_DIR}/usr/local/sbin/mihomo-refresh"      /usr/local/sbin/mihomo-refresh
install -m 755 "${SOURCE_DIR}/usr/local/sbin/check-route"       /usr/local/sbin/check-route
echo "  -> скрипты установлены в /usr/local/sbin/"

if [ "$MODE" = "fresh" ]; then
  echo "=== 6. Запрашиваем ссылки у пользователя ==="
  read -r -p "Введите ссылку на подписку (subscription.url): " SUBSCRIPTION_URL
  if [ -z "${SUBSCRIPTION_URL}" ]; then
    echo "Пустая ссылка на подписку. Установка прервана."
    exit 1
  fi
  printf '%s\n' "${SUBSCRIPTION_URL}" > /etc/mihomo/subscription.url

  read -r -p "Введите ссылку на обновление маршрутов (routing-rules.url): " ROUTING_RULES_URL
  if [ -z "${ROUTING_RULES_URL}" ]; then
    echo "Пустая ссылка на маршруты. Установка прервана."
    exit 1
  fi
  printf '%s\n' "${ROUTING_RULES_URL}" > /etc/mihomo/routing-rules.url
  chown root:mihomo /etc/mihomo/subscription.url /etc/mihomo/routing-rules.url
  chmod 640 /etc/mihomo/subscription.url /etc/mihomo/routing-rules.url
  echo "  -> ссылки сохранены в /etc/mihomo/"
else
  echo "=== 6. Пропускаем запрос ссылок (режим update) ==="
fi

echo "=== 7. Устанавливаем systemd-сервис ==="
install -m 644 "${SOURCE_DIR}/etc/systemd/system/mihomo.service" /etc/systemd/system/mihomo.service
systemctl daemon-reload
echo "  -> mihomo.service установлен"

echo "=== 8. Устанавливаем cron ==="
install -m 644 "${SOURCE_DIR}/etc/cron.d/mihomo-refresh" /etc/cron.d/mihomo-refresh
if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files | awk '{print $1}' | grep -qx "cron.service"; then
    systemctl enable --now cron >/dev/null 2>&1 || true
  elif systemctl list-unit-files | awk '{print $1}' | grep -qx "crond.service"; then
    systemctl enable --now crond >/dev/null 2>&1 || true
  fi
fi
echo "  -> cron установлен (обновление подписки каждую минуту)"

echo "=== 9. Запускаем ==="
systemctl enable mihomo
if [ "$MODE" = "fresh" ]; then
  systemctl start mihomo
  echo "  -> mihomo запущен и включён в автозагрузку"
else
  systemctl restart mihomo
  echo "  -> mihomo перезапущен с новыми скриптами/конфигом"
fi

echo ""
echo "Готово! Проверка: systemctl status mihomo"
echo "Логи:    journalctl -u mihomo -f"
echo "API:     curl http://127.0.0.1:9090"
