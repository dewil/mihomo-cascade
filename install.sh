#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Шаг планировщика обновления: пара systemd-юнитов вместо прежнего /etc/cron.d.
#
# Почему функция и почему в самом начале файла. Шаг обязан быть проверяемым без
# установки в систему: тесты (tests/scheduler-install.sh) зовут его отдельно,
# `install.sh --step scheduler`, без прав root и без скачиваний. Разбор этого
# аргумента идет до всего остального - до определения архитектуры и до проверки
# root, иначе шаг не выполнить на машине, где нет ни того, ни другого.
#
# DESTDIR - префикс всех путей файловой системы на этом шаге. Пустой при боевой
# установке, каталог песочницы в тестах: боевой /etc при прогоне тестов не
# трогается вовсе.
# Шаг планировщика отдельной функцией: его зовет и обычная установка, и
# `install.sh --step scheduler` - режим для тестов и для доустановки на уже
# развернутой ноде. На чистой машине этим режимом планировщик поднимать не надо:
# сам /usr/local/sbin/mihomo-refresh ставится шагом 5, и без него таймер будет
# исправно запускать несуществующую команду раз в две минуты.
install_scheduler() {
  local dest="${DESTDIR:-}"
  local src="${SOURCE_DIR:-$SCRIPT_DIR}"
  local units_dir="${dest}/etc/systemd/system"
  local cron_legacy="${dest}/etc/cron.d/mihomo-refresh"

  # Без systemd ставить планировщик некуда, и молчаливое продолжение тут
  # недопустимо: ровно так стоял home-server - файл расписания на месте, демона
  # нет, конфиг заморожен месяц, а установка отрапортовала успех.
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "ОШИБКА: systemctl не найден, systemd в системе нет - планировщик обновления поставить некуда." >&2
    return 1
  fi

  install -d -m 755 "${units_dir}"
  install -m 644 "${src}/etc/systemd/system/mihomo-refresh.service" "${units_dir}/mihomo-refresh.service"
  install -m 644 "${src}/etc/systemd/system/mihomo-refresh.timer"   "${units_dir}/mihomo-refresh.timer"

  if ! systemctl daemon-reload; then
    echo "ОШИБКА: systemctl daemon-reload не отработал; юниты уложены в ${units_dir}, таймер не поднят. Прежнее расписание cron.d не тронуто - обновление на ноде продолжает работать по нему." >&2
    return 1
  fi
  if ! systemctl enable --now mihomo-refresh.timer; then
    echo "ОШИБКА: не удалось включить и запустить mihomo-refresh.timer; юниты уложены в ${units_dir}, прежнее расписание cron.d не тронуто." >&2
    return 1
  fi
  # enable --now не перезапускает уже активный таймер, поэтому на обновлении
  # новое расписание вступило бы в силу только после перезагрузки машины.
  if ! systemctl restart mihomo-refresh.timer; then
    echo "ОШИБКА: mihomo-refresh.timer не перезапустился с новым расписанием; юниты уложены в ${units_dir}, прежнее расписание cron.d не тронуто." >&2
    return 1
  fi

  # Спрашиваем систему о фактическом состоянии, а не печатаем успех по факту
  # того, что команды были отданы: прежний шаг выглядел одинаково на машине, где
  # обновление работает, и на машине, где его не будет никогда.
  if ! systemctl is-enabled mihomo-refresh.timer >/dev/null 2>&1; then
    echo "ОШИБКА: mihomo-refresh.timer не включен в автозагрузку (systemctl is-enabled) - после перезагрузки нода обновляться не будет. Юниты уложены в ${units_dir}, прежнее расписание cron.d не тронуто." >&2
    return 1
  fi
  if ! systemctl is-active mihomo-refresh.timer >/dev/null 2>&1; then
    echo "ОШИБКА: mihomo-refresh.timer не активен (systemctl is-active) - нода не обновляется прямо сейчас. Юниты уложены в ${units_dir}, прежнее расписание cron.d не тронуто." >&2
    return 1
  fi

  # Прежнее расписание снимается ТОЛЬКО здесь - после того, как таймер проверен
  # живым. Порядок не косметический: если снять cron раньше и споткнуться на
  # любой из проверок выше, нода останется вообще без планировщика, то есть в
  # состоянии хуже, чем до запуска установщика. Пока проверки не прошли, старый
  # cron продолжает обновлять ноду, и откат сводится к "ничего не делать".
  #
  # Трогаем ровно свой файл - в crontab пользователя root мы никогда не писали,
  # и удалять чужие записи по совпадению имени не будем.
  if [ -e "${cron_legacy}" ]; then
    rm -f "${cron_legacy}"
    echo "  -> снят прежний ${cron_legacy#"$dest"} (его заменил таймер)"
  fi

  echo "  -> планировщик: systemd timer, раз в 2 мин со случайной задержкой до 59 с"
}

if [ "${1:-}" = "--step" ]; then
  case "${2:-}" in
    scheduler) install_scheduler || exit $?; exit 0 ;;
    *) echo "Неизвестный шаг: ${2:-<пусто>} (известен: scheduler)" >&2; exit 1 ;;
  esac
fi

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

# Релиз mihomo для amd64 собран под микроархитектуру x86-64-v3 (AVX2, BMI2, FMA).
# На процессоре ниже этого уровня бинарь не стартует вовсе: "This program can only
# be run on AMD64 processors with v3 microarchitecture support", и systemd уходит
# в бесконечный рестарт. Для таких машин upstream публикует сборку -compatible.
# Всплыло на домашней ноде (Celeron J1900, x86-64-v2); на серверных CPU не видно.
supports_x86_64_v3() {
  local ldso
  for ldso in /lib64/ld-linux-x86-64.so.2 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2; do
    [ -x "$ldso" ] || continue
    # glibc 2.33+ перечисляет поддерживаемые уровни в --help
    if "$ldso" --help 2>/dev/null | grep -q 'x86-64-v3 (supported)'; then
      return 0
    fi
    if "$ldso" --help 2>/dev/null | grep -q 'x86-64-v[0-9] (supported)'; then
      return 1   # ld.so ответил, но v3 в списке нет
    fi
  done
  # Старая glibc: решаем по флагам CPU - три из набора v3 достаточно показательны
  grep -qw avx2 /proc/cpuinfo && grep -qw bmi2 /proc/cpuinfo && grep -qw fma /proc/cpuinfo
}

if [ "$ARCH_DL" = "amd64" ] && ! supports_x86_64_v3; then
  ARCH_DL="amd64-compatible"
fi

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
  mkdir -p "${TMP_FETCH}/etc/mihomo" "${TMP_FETCH}/etc/systemd/system" "${TMP_FETCH}/usr/local/sbin"

  fetch_from_github "etc/mihomo/config.base.yaml" "${TMP_FETCH}/etc/mihomo/config.base.yaml"
  fetch_from_github "etc/mihomo/subscription.url" "${TMP_FETCH}/etc/mihomo/subscription.url"
  fetch_from_github "etc/mihomo/routing-rules.url" "${TMP_FETCH}/etc/mihomo/routing-rules.url"
  fetch_from_github "etc/mihomo/routing-rules.yaml" "${TMP_FETCH}/etc/mihomo/routing-rules.yaml"
  fetch_from_github "etc/mihomo/iso3166_alpha2.txt" "${TMP_FETCH}/etc/mihomo/iso3166_alpha2.txt"
  fetch_from_github "etc/mihomo/local-rules.yaml" "${TMP_FETCH}/etc/mihomo/local-rules.yaml"
  fetch_from_github "etc/systemd/system/mihomo.service" "${TMP_FETCH}/etc/systemd/system/mihomo.service"
  fetch_from_github "etc/systemd/system/mihomo-refresh.service" "${TMP_FETCH}/etc/systemd/system/mihomo-refresh.service"
  fetch_from_github "etc/systemd/system/mihomo-refresh.timer" "${TMP_FETCH}/etc/systemd/system/mihomo-refresh.timer"
  fetch_from_github "usr/local/sbin/mihomo-build-config" "${TMP_FETCH}/usr/local/sbin/mihomo-build-config"
  fetch_from_github "usr/local/sbin/mihomo-refresh" "${TMP_FETCH}/usr/local/sbin/mihomo-refresh"
  fetch_from_github "usr/local/sbin/check-route" "${TMP_FETCH}/usr/local/sbin/check-route"
  fetch_from_github "usr/local/sbin/mihomo-api" "${TMP_FETCH}/usr/local/sbin/mihomo-api"

  chmod 755 "${TMP_FETCH}/usr/local/sbin/mihomo-build-config" "${TMP_FETCH}/usr/local/sbin/mihomo-refresh" "${TMP_FETCH}/usr/local/sbin/check-route" "${TMP_FETCH}/usr/local/sbin/mihomo-api"
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
install -m 755 "${SOURCE_DIR}/usr/local/sbin/mihomo-api"        /usr/local/sbin/mihomo-api
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

echo "=== 8. Устанавливаем планировщик обновления ==="
# Ошибка шага роняет установку (set -e): нода без планировщика не обновляется,
# и узнать об этом по выводу установщика раньше было нельзя.
install_scheduler

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
