#!/usr/bin/env bash
# CASC-TIMER: проверка шага установки планировщика обновления (systemd timer).
# Покрывает FR-CASC-TIMER-01..07 из docs/dev/2026-09-08-spec-casc-timer.md.
#
# Запуск:  bash tests/scheduler-install.sh
# Требует: bash, coreutils. Права root НЕ нужны и запрещены (см. гейт ниже):
# все записи идут в песочницу через DESTDIR, боевой /etc не трогается.
#
# Контракт, на который опирается тест (требования к реализации, не догадки о ней):
#   1. DESTDIR - префикс всех путей файловой системы на этом шаге:
#      юниты в $DESTDIR/etc/systemd/system/, уходящий cron в $DESTDIR/etc/cron.d/.
#   2. `bash install.sh --step scheduler` выполняет ТОЛЬКО шаг планировщика,
#      без скачиваний и без прав root, и возвращает код этого шага.
#   3. Наличие systemd определяется поиском systemctl в PATH.
#   4. Фактическое состояние таймера проверяется через
#      `systemctl is-enabled mihomo-refresh.timer` и `systemctl is-active ...`
#      (заглушка понимает и `systemctl show -p UnitFileState|ActiveState`).
#   5. Успех печатается строкой, называющей расписание ("2 мин" / "две минуты");
#      при провале такой строки в выводе нет.
#
# ЛОВУШКА: на боевой ноде /etc/cron.d/mihomo-refresh существует по-настоящему.
# Тест обязан удалять только копию внутри песочницы, поэтому запуск от root
# запрещен явно - иначе ошибка в реализации снесла бы живое расписание.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT/install.sh"
SB="$(mktemp -d)"
trap 'rm -rf "$SB"' EXIT

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "ОТКАЗ: тест не запускается от root - шаг установки может писать мимо DESTDIR." >&2
  exit 2
fi

FAILED=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; FAILED=1; }
check(){ [[ "$2" == "$3" ]] && ok "$1 ($2)" || bad "$1: ожидалось [$3], получено [$2]"; }
# grep -c при нуле совпадений печатает 0 И возвращает 1 - см. tests/stub-policy.sh.
grepq(){ grep -Eqi "$1" "$2" 2>/dev/null && echo да || echo нет; }

# --- стенд -------------------------------------------------------------------
mkdir -p "$SB/bin" "$SB/purebin" "$SB/root"
LOG="$SB/systemctl.log"
MODEF="$SB/mode"

cat > "$SB/bin/systemctl" <<STUB
#!/usr/bin/env bash
# Подставной systemctl: пишет вызовы в лог, коды возврата задает тест файлом mode.
printf '%s\n' "\$*" >> "$LOG"
MODE="\$(cat "$MODEF" 2>/dev/null || echo ok)"
CMD=""
for a in "\$@"; do case "\$a" in -*) ;; *) CMD="\$a"; break;; esac; done
case "\$CMD" in
  is-enabled)
    if [[ "\$MODE" == notenabled ]]; then echo disabled; exit 1; fi
    echo enabled; exit 0 ;;
  is-active)
    if [[ "\$MODE" == notactive ]]; then echo inactive; exit 3; fi
    echo active; exit 0 ;;
  show)
    if [[ "\$MODE" == notenabled ]]; then echo "UnitFileState=disabled"; else echo "UnitFileState=enabled"; fi
    if [[ "\$MODE" == notactive ]];  then echo "ActiveState=inactive";   else echo "ActiveState=active";   fi
    exit 0 ;;
  enable|start|restart)
    if [[ "\$MODE" == enablefail ]]; then echo "Failed to enable unit: stub" >&2; exit 1; fi
    exit 0 ;;
esac
exit 0
STUB
chmod +x "$SB/bin/systemctl"

# purebin - PATH без systemctl вообще (сценарий "systemd в системе нет").
for t in bash sh cat cp mv rm mkdir rmdir ln chmod chown install sed grep egrep awk \
         cut tr head tail sort uniq wc date id dirname basename find printf env true false \
         sleep touch stat readlink realpath tee xargs python3 curl; do
  p="$(command -v "$t" 2>/dev/null)" && ln -sf "$p" "$SB/purebin/$t"
done

UNITS="$SB/root/etc/systemd/system"
CRON="$SB/root/etc/cron.d/mihomo-refresh"
OUT="$SB/out.txt"

reset() { # $1 = режим заглушки
  rm -rf "$SB/root"; mkdir -p "$SB/root/etc"
  : > "$LOG"; printf '%s\n' "$1" > "$MODEF"
}
invoke() { # [$1 = nosystemctl] ; печатает код возврата, вывод в $OUT
  local path="$SB/bin:$PATH"
  [[ "${1:-}" == nosystemctl ]] && path="$SB/purebin"
  env PATH="$path" DESTDIR="$SB/root" bash "$INSTALLER" --step scheduler >"$OUT" 2>&1
  echo $?
}
unit() { cat "$1" 2>/dev/null | tr -d ' \t'; }   # сравнение без оглядки на пробелы

REAL_CRON_BEFORE=$([[ -e /etc/cron.d/mihomo-refresh ]] && echo есть || echo нет)

echo "A. FR-CASC-TIMER-01: юниты установлены, таймер включен и запущен"
reset ok; RC=$(invoke)
check "код возврата" "$RC" "0"
check "mihomo-refresh.service на месте" "$([[ -f "$UNITS/mihomo-refresh.service" ]] && echo да || echo нет)" "да"
check "mihomo-refresh.timer на месте"   "$([[ -f "$UNITS/mihomo-refresh.timer" ]] && echo да || echo нет)" "да"
check "был daemon-reload" "$(grepq 'daemon-reload' "$LOG")" "да"
check "таймер включен в автозагрузку" "$(grepq '(^| )enable( .*)? mihomo-refresh\.timer' "$LOG")" "да"
check "таймер запущен сейчас" \
  "$(grepq '(enable .*--now .*mihomo-refresh\.timer|--now .*enable .*mihomo-refresh\.timer|(^| )start( .*)? mihomo-refresh\.timer)' "$LOG")" "да"
check "результат проверен фактически" "$(grepq '(is-active|is-enabled|show).*mihomo-refresh\.timer' "$LOG")" "да"

echo "B. FR-CASC-TIMER-02: расписание и таймаут в самих юнитах"
check "интервал две минуты" \
  "$(unit "$UNITS/mihomo-refresh.timer" | grep -Eqi '^(OnUnitActiveSec|OnUnitInactiveSec)=(2min|2m|120s?)$|^OnCalendar=\*:0/2$' && echo да || echo нет)" "да"
check "случайная задержка до минуты" \
  "$(unit "$UNITS/mihomo-refresh.timer" | grep -Eqi '^RandomizedDelaySec=(59s?|60s?|1min|1m)$' && echo да || echo нет)" "да"
# Дописано 08.09.2026 после уточнения спеки: сам джиттер бесполезен без явной
# точности. По умолчанию systemd вправе сдвигать запуск в окне до минуты, группируя
# пробуждения, - и эта группировка возвращает синхронный залп машин, против которого
# сделана вся фича. Проверка мутационная: снятие строки AccuracySec из юнита обязано
# красить тест.
check "точность задана явно (иначе группировка съедает джиттер)" \
  "$(unit "$UNITS/mihomo-refresh.timer" | grep -Eqi '^AccuracySec=(1s?|1sec)$' && echo да || echo нет)" "да"
check "таймаут прогона 300 с" \
  "$(unit "$UNITS/mihomo-refresh.service" | grep -Eqi '^TimeoutStartSec=(300s?|5min|5m)$' && echo да || echo нет)" "да"
check "запускается команда обновления" \
  "$(grepq '^ExecStart=.*/usr/local/sbin/mihomo-refresh' "$UNITS/mihomo-refresh.service")" "да"
check "таймер привязан к своему сервису" \
  "$(unit "$UNITS/mihomo-refresh.timer" | grep -Eqi '^Unit=mihomo-refresh\.service$' && echo да || echo нет; )" "да"

echo "C. FR-CASC-TIMER-07: наложение прогонов исключено механизмом, не флоком"
check "Type=oneshot" "$(unit "$UNITS/mihomo-refresh.service" | grep -Eqi '^Type=oneshot$' && echo да || echo нет)" "да"
check "зависший прогон снимается по таймауту" \
  "$(unit "$UNITS/mihomo-refresh.service" | grep -Eqi '^TimeoutStartSec=' && echo да || echo нет)" "да"
check "таймаут не бесконечный" \
  "$(unit "$UNITS/mihomo-refresh.service" | grep -Eqi '^TimeoutStartSec=(0|infinity)$' && echo да || echo нет)" "нет"
check "ExecStart без flock" "$(grepq '^ExecStart=.*flock' "$UNITS/mihomo-refresh.service")" "нет"

echo "D. FR-CASC-TIMER-06: итог называет фактическое расписание"
check "названо реальное расписание" "$(grepq '(две минуты|2 мин|2 минут)' "$OUT")" "да"
check "нет вранья про каждую минуту" "$(grepq 'каждую минуту' "$OUT")" "нет"
check "нет рапорта про cron" "$(grepq 'cron установлен' "$OUT")" "нет"

echo "E. FR-CASC-TIMER-05: прежний cron.d снимается, планировщик остается один"
reset ok
mkdir -p "$SB/root/etc/cron.d"
printf '*/2 * * * * root /usr/local/sbin/mihomo-refresh\n' > "$CRON"
RC=$(invoke)
check "код возврата" "$RC" "0"
check "старый cron.d удален" "$([[ -e "$CRON" ]] && echo есть || echo нет)" "нет"
check "таймер поставлен" "$([[ -f "$UNITS/mihomo-refresh.timer" ]] && echo да || echo нет)" "да"
# Считать просто "планировщиков одна штука" нельзя: состояние "cron остался,
# таймера нет" дало бы ту же единицу - проверка молчала бы одинаково в исправном
# и сломанном состоянии. Поэтому фиксируем, КАКОЙ именно остался.
check "остался ровно один планировщик - таймер" \
  "cron=$([[ -e "$CRON" ]] && echo есть || echo нет),timer=$([[ -f "$UNITS/mihomo-refresh.timer" ]] && echo есть || echo нет)" \
  "cron=нет,timer=есть"

echo "F. FR-CASC-TIMER-03: таймер не активен - провал с причиной, без рапорта об успехе"
reset notactive; RC=$(invoke)
check "код возврата ненулевой" "$([[ "$RC" -ne 0 ]] && echo да || echo нет)" "да"
check "причина названа в выводе" "$(grepq '(mihomo-refresh|таймер|timer)' "$OUT")" "да"
check "успеха не рапортует" "$(grepq '(две минуты|2 мин|2 минут)' "$OUT")" "нет"

echo "G. FR-CASC-TIMER-03: таймер не в автозагрузке - тот же провал"
reset notenabled; RC=$(invoke)
check "код возврата ненулевой" "$([[ "$RC" -ne 0 ]] && echo да || echo нет)" "да"
check "причина названа в выводе" "$(grepq '(автозагруз|enable|включ)' "$OUT")" "да"
check "успеха не рапортует" "$(grepq '(две минуты|2 мин|2 минут)' "$OUT")" "нет"

echo "H. FR-CASC-TIMER-03: enable упал - шаг не проглатывает ошибку"
reset enablefail; RC=$(invoke)
check "код возврата ненулевой" "$([[ "$RC" -ne 0 ]] && echo да || echo нет)" "да"
check "причина названа в выводе" "$(grepq '(mihomo-refresh|таймер|timer|enable|включ)' "$OUT")" "да"
check "успеха не рапортует" "$(grepq '(две минуты|2 мин|2 минут)' "$OUT")" "нет"

echo "I. FR-CASC-TIMER-04: systemd в системе нет - явный отказ, а не молчание"
reset ok; RC=$(invoke nosystemctl)
check "код возврата ненулевой" "$([[ "$RC" -ne 0 ]] && echo да || echo нет)" "да"
check "сказано про systemd" "$(grepq '(systemd|systemctl)' "$OUT")" "да"
check "успеха не рапортует" "$(grepq '(две минуты|2 мин|2 минут)' "$OUT")" "нет"

echo "J. песочница: боевая файловая система не тронута"
check "боевой /etc/cron.d/mihomo-refresh на месте" \
  "$([[ -e /etc/cron.d/mihomo-refresh ]] && echo есть || echo нет)" "$REAL_CRON_BEFORE"
check "боевых юнитов не появилось" \
  "$([[ -e /etc/systemd/system/mihomo-refresh.timer || -e /etc/systemd/system/mihomo-refresh.service ]] && echo да || echo нет)" "нет"

echo
[[ $FAILED -eq 0 ]] && echo "ВСЕ СЦЕНАРИИ ПРОШЛИ" || echo "ЕСТЬ ПРОВАЛЫ"
exit $FAILED
