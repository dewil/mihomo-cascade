#!/usr/bin/env bash
# CASC-FRESH: установка на чистую машину доходит до конца, служба может создать
# свой кэш, а каталог конфигурации остается недоступным ей на запись.
# Покрывает FR-CASC-FRESH-01..03,05 (критерии приемки) из
# docs/dev/2026-09-22-spec-casc-fresh-install.md. FR-04 (миграция боевой ноды с
# существующим cache.db) и FR-06 (README) в этот файл не входят - их нет в
# перечне "Критерии приемки" самой спеки, и оба требуют либо живой ноды, либо
# ревью текста, а не проверки шагом установщика.
#
# Запуск:  bash tests/fresh-install.sh
# Требует: bash, coreutils (stat с форматом -c). Права root НЕ нужны и
# запрещены (см. гейт ниже): все записи идут в песочницу через DESTDIR,
# боевые /etc и /usr/local не трогаются.
#
# Контракт, на который опирается тест (требования к реализации, а не догадки о
# ней - имена шагов и путь состояния тест ФИКСИРУЕТ как часть контракта):
#   1. DESTDIR - префикс всех путей файловой системы, как у install_scheduler:
#      скрипты в $DESTDIR/usr/local/sbin/, каталог состояния в
#      $DESTDIR/var/lib/mihomo, юнит службы в $DESTDIR/etc/systemd/system/.
#   2. `bash install.sh --step scripts` устанавливает ровно четыре скрипта
#      (mihomo-build-config, mihomo-refresh, check-route, mihomo-api) в
#      $DESTDIR/usr/local/sbin/, создавая каталог, если его нет; без прав root
#      и без скачиваний. Возвращает код этого шага.
#   3. `bash install.sh --step state` создает каталог состояния
#      $DESTDIR/var/lib/mihomo - ОТДЕЛЬНО от каталога конфигурации
#      $DESTDIR/etc/mihomo (размен спеки: "состояние появляется отдельным
#      каталогом"), с правами, дающими владельцу и/или группе каталога право
#      писать в него. Каталог конфигурации этот шаг не трогает вовсе - ни
#      права, ни владельца, ни группу.
#   4. Оба шага НЕ требуют существования системного пользователя/группы
#      "mihomo" для собственной проверки: тест не может без root ни создать,
#      ни подтвердить владение произвольным системным пользователем, поэтому
#      здесь проверяются НАБЛЮДАЕМЫЕ следствия (биты прав, неизменность
#      каталога конфигурации, реальная возможность создать файл), а не
#      буквальное имя владельца.
#   5. `--step state` устанавливает (или переустанавливает) юнит
#      $DESTDIR/etc/systemd/system/mihomo.service, и в его содержимом есть
#      связь с каталогом состояния - либо директива systemd
#      `StateDirectory=<basename каталога>`, либо буквальный абсолютный путь
#      каталога состояния где-то в файле (ExecStartPre, Environment,
#      комментарий рядом с механизмом). Тест принимает любую из двух форм.
#
# ОТКРЫТЫЙ ВОПРОС К СПЕКЕ (см. отчет): пункт 5 контракта - предположение.
# Спека говорит только "юнит несет связь со состоянием, проверяется по факту
# в песочнице", не называя ни шаг, который его туда кладет, ни механизм
# привязки. Если реализация решит устанавливать юнит другим шагом или вовсе
# оставить его установку в существующей (не-DESTDIR, root-only) ветке
# install.sh - секция E ниже перестанет быть выполнимой по спеке буквально и
# нуждается в пересмотре вместе с автором реализации.
#
# ЛОВУШКА, как в tests/scheduler-install.sh: реальные /etc/mihomo и
# /usr/local/sbin на этой машине могут существовать. Тест обязан работать
# только внутри $SB - поэтому запуск от root запрещен явно.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT/install.sh"
SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "ОТКАЗ: тест не запускается от root - шаги установки могут писать мимо DESTDIR." >&2
  exit 2
fi

FAILED=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; FAILED=1; }
check(){ [[ "$2" == "$3" ]] && ok "$1 ($2)" || bad "$1: ожидалось [$3], получено [$2]"; }
# grep -c при нуле совпадений печатает 0 И возвращает 1 - см. tests/stub-policy.sh.
grepq(){ grep -Eqi "$1" "$2" 2>/dev/null && echo да || echo нет; }

ROOT_SB="$SB/root"
CONFIG_DIR="$ROOT_SB/etc/mihomo"
SCRIPTS_DIR="$ROOT_SB/usr/local/sbin"
STATE_DIR="$ROOT_SB/var/lib/mihomo"
UNIT_FILE="$ROOT_SB/etc/systemd/system/mihomo.service"
OUT="$SB/out.txt"

reset() { rm -rf "$ROOT_SB"; mkdir -p "$ROOT_SB/etc"; }
invoke() { # $1 = шаг ("scripts" | "state"); печатает код возврата, вывод в $OUT
  env DESTDIR="$ROOT_SB" SOURCE_DIR="$ROOT" bash "$INSTALLER" --step "$1" >"$OUT" 2>&1
  echo $?
}
seed_config_dir() { # готовит каталог конфигурации, как это делает боевая установка
  install -d -m 750 "$CONFIG_DIR"
}
snap() { stat -c '%a %U %G' "$1" 2>/dev/null; }   # "mode owner group" одной строкой
write_bits() { # 1 - есть бит w у group ИЛИ other, 0 - нет ни того ни другого
  local mode; mode="$(stat -c '%a' "$1" 2>/dev/null)"
  local go="${mode: -2}"
  [[ "${go:0:1}" =~ [2367] || "${go:1:1}" =~ [2367] ]] && echo да || echo нет
}

REAL_SCRIPTS_BEFORE=$([[ -e /usr/local/sbin/mihomo-refresh ]] && echo есть || echo нет)
REAL_CONFIG_BEFORE=$([[ -e /etc/mihomo/config.base.yaml ]] && echo есть || echo нет)

echo "A. FR-CASC-FRESH-01: каталог для скриптов отсутствует - шаг его создает"
reset; RC=$(invoke scripts)
check "код возврата" "$RC" "0"
check "каталог скриптов создан" "$([[ -d "$SCRIPTS_DIR" ]] && echo да || echo нет)" "да"
for s in mihomo-build-config mihomo-refresh check-route mihomo-api; do
  check "скрипт $s поставлен и исполняем" \
    "$([[ -x "$SCRIPTS_DIR/$s" ]] && echo да || echo нет)" "да"
done

echo "B. FR-CASC-FRESH-01: каталог для скриптов уже есть - повторный прогон не меняет его прав"
chmod 700 "$SCRIPTS_DIR"   # нарочно нестандартный режим - шаг не имеет права его "поправить"
BEFORE=$(snap "$SCRIPTS_DIR")
RC=$(invoke scripts)
check "код возврата" "$RC" "0"
check "права каталога скриптов не изменились" "$(snap "$SCRIPTS_DIR")" "$BEFORE"
check "скрипты обновлены при повторном прогоне" \
  "$([[ -x "$SCRIPTS_DIR/mihomo-api" ]] && echo да || echo нет)" "да"

echo "C. FR-CASC-FRESH-02/03: подготовка состояния создает отдельный писабельный каталог,"
echo "   а каталог конфигурации остается нетронутым и недоступным на запись"
reset; seed_config_dir
CONFIG_BEFORE=$(snap "$CONFIG_DIR")
RC=$(invoke state)
check "код возврата" "$RC" "0"
check "каталог состояния создан" "$([[ -d "$STATE_DIR" ]] && echo да || echo нет)" "да"
# Отдельность - не техническая деталь: спека явно отвергает решение "внутри
# каталога конфигурации" (размен "состояние появляется отдельным каталогом").
check "каталог состояния не совпадает и не лежит внутри каталога конфигурации" \
  "$([[ "$STATE_DIR" != "$CONFIG_DIR" && "$STATE_DIR" != "$CONFIG_DIR"/* ]] && echo да || echo нет)" "да"
# Функциональная, а не только символьная проверка: реально пробуем создать файл.
check "в каталоге состояния реально можно создать файл" \
  "$(: > "$STATE_DIR/.probe" 2>/dev/null && echo да || echo нет)" "да"
rm -f "$STATE_DIR/.probe"
# Главная проверка FR-03: не "права каталога конфигурации сейчас без записи"
# (это верно и для решения "chown -R mihomo:mihomo /etc/mihomo", если проверить
# ДО него), а "шаг состояния его вообще не трогал" - иначе проверка молчала бы
# одинаково и при исправном шаге, и при обходном пути из спеки.
check "каталог конфигурации не изменился ни на бит" "$(snap "$CONFIG_DIR")" "$CONFIG_BEFORE"
check "у каталога конфигурации нет бита записи для group/other" \
  "$(write_bits "$CONFIG_DIR")" "нет"

echo "D. идемпотентность: прогон дважды подряд ничего не ломает и не меняет"
reset; seed_config_dir
RC1=$(invoke scripts); OUT1="$(cat "$OUT")"
RC2=$(invoke state);   OUT2="$(cat "$OUT")"
STATE_SNAP_1=$(snap "$STATE_DIR"); CONFIG_SNAP_1=$(snap "$CONFIG_DIR")
RC3=$(invoke scripts)
RC4=$(invoke state)
check "коды возврата второго прогона тоже нулевые" "rc3=$RC3,rc4=$RC4" "rc3=0,rc4=0"
check "каталог состояния не изменился на втором прогоне" "$(snap "$STATE_DIR")" "$STATE_SNAP_1"
check "каталог конфигурации не изменился на втором прогоне" "$(snap "$CONFIG_DIR")" "$CONFIG_SNAP_1"
check "второй прогон не жалуется в вывод" "$(grepq '(ОШИБКА|ПРЕДУПРЕЖДЕНИЕ)' "$OUT")" "нет"

echo "E. FR-CASC-FRESH-05: юнит службы после установки несет связь с каталогом состояния"
echo "   (см. 'ОТКРЫТЫЙ ВОПРОС' в шапке файла - это предположение о том, какой шаг ставит юнит)"
reset; RC=$(invoke state)
check "код возврата" "$RC" "0"
check "юнит службы установлен в песочницу" "$([[ -f "$UNIT_FILE" ]] && echo да || echo нет)" "да"
STATE_BASENAME="$(basename "$STATE_DIR")"
check "юнит связан с каталогом состояния (StateDirectory= либо буквальный путь)" \
  "$(grepq "(^StateDirectory=.*(^|,)${STATE_BASENAME}(,|\$)|$(printf '%s' "$STATE_DIR" | sed 's/[.[\*^$/]/\\&/g'))" "$UNIT_FILE")" "да"

echo "F. песочница: боевая файловая система не тронута"
check "боевой /usr/local/sbin/mihomo-refresh на месте как был" \
  "$([[ -e /usr/local/sbin/mihomo-refresh ]] && echo есть || echo нет)" "$REAL_SCRIPTS_BEFORE"
check "боевой /etc/mihomo/config.base.yaml на месте как был" \
  "$([[ -e /etc/mihomo/config.base.yaml ]] && echo есть || echo нет)" "$REAL_CONFIG_BEFORE"
check "боевого /var/lib/mihomo не появилось" \
  "$([[ -e /var/lib/mihomo ]] && echo есть || echo нет)" "нет"

echo
[[ $FAILED -eq 0 ]] && echo "ВСЕ СЦЕНАРИИ ПРОШЛИ" || echo "ЕСТЬ ПРОВАЛЫ"
exit $FAILED
