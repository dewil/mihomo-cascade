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

# Подставные install и rm. Нужны находке 1 сверки: шаг зовется как
# `install_scheduler || exit $?`, и bash в таком контексте снимает errexit со
# всего тела функции - значит проваленные файловые операции надо уметь
# воспроизвести, а не рассуждать о них. В штатных режимах стуб прозрачен и
# просто вызывает настоящую команду.
REAL_INSTALL="$(command -v install)"
REAL_RM="$(command -v rm)"
cat > "$SB/bin/install" <<STUB
#!/usr/bin/env bash
MODE="\$(cat "$MODEF" 2>/dev/null || echo ok)"
if [[ "\$MODE" == installfail ]]; then
  echo "install: cannot create regular file: stub failure" >&2
  exit 1
fi
exec "$REAL_INSTALL" "\$@"
STUB
cat > "$SB/bin/rm" <<STUB
#!/usr/bin/env bash
MODE="\$(cat "$MODEF" 2>/dev/null || echo ok)"
if [[ "\$MODE" == rmfail ]]; then
  echo "rm: cannot remove: stub failure" >&2
  exit 1
fi
exec "$REAL_RM" "\$@"
STUB
chmod +x "$SB/bin/install" "$SB/bin/rm"

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
SRC=""   # каталог-источник юнитов; пусто = сам репозиторий
invoke() { # [$1 = nosystemctl] ; печатает код возврата, вывод в $OUT
  local path="$SB/bin:$PATH"
  [[ "${1:-}" == nosystemctl ]] && path="$SB/purebin"
  env PATH="$path" DESTDIR="$SB/root" SOURCE_DIR="${SRC:-$ROOT}" \
    bash "$INSTALLER" --step scheduler >"$OUT" 2>&1
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
# Перевернуто 08.09.2026 по находке 2 сверки. Прежняя редакция требовала
# ExecStart БЕЗ flock и опиралась на спеку ("systemd не стартует второй экземпляр
# своего юнита, лок не нужен"). Утверждение верно только для экземпляров этого
# юнита: старый cron-прогон в окне миграции ему не подчиняется, а временные файлы
# у них общие. Лок вернулся, спека уточнена - см. "Уточнения после сверки".
check "ExecStart берет лок" "$(grepq '^ExecStart=.*flock' "$UNITS/mihomo-refresh.service")" "да"
check "лок тот же, что брал прежний крон" \
  "$(grepq '^ExecStart=.*/run/mihomo-refresh\.lock' "$UNITS/mihomo-refresh.service")" "да"

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

echo "K. провал проверки не оставляет ноду без планировщика вовсе"
# Дописано 08.09.2026 по находке сверки. Прежний порядок снимал cron.d до
# проверок, и любая красная проверка оставляла машину в состоянии хуже, чем до
# установки: таймер не поднялся, а старое расписание уже удалено. Поэтому cron
# снимается последним - пока таймер не подтвержден живым, обновление на ноде
# продолжает идти по прежнему расписанию, и откат сводится к бездействию.
for mode in notactive notenabled enablefail; do
  reset "$mode"
  mkdir -p "$SB/root/etc/cron.d"
  printf '*/2 * * * * root /usr/local/sbin/mihomo-refresh\n' > "$CRON"
  RC=$(invoke)
  check "[$mode] код возврата ненулевой" "$([[ "$RC" -ne 0 ]] && echo да || echo нет)" "да"
  check "[$mode] прежний cron.d на месте" "$([[ -e "$CRON" ]] && echo есть || echo нет)" "есть"
  check "[$mode] в выводе сказано, что старое расписание цело" "$(grepq '(cron\.d не тронут|не тронуто)' "$OUT")" "да"
done

echo "L. находка 1 сверки: провал файловой операции не выдается за успех"
# Шаг вызывается как `install_scheduler || exit $?`, и bash в таком контексте
# отключает errexit внутри всей функции: без явных проверок проваленные install и
# rm доезжают до строки успеха, шаг возвращает 0, а вывод вдобавок утверждает, что
# cron снят. Оба провала воспроизводятся подставными командами в PATH песочницы.
reset installfail; RC=$(invoke)
check "[install] код возврата ненулевой" "$([[ "$RC" -ne 0 ]] && echo да || echo нет)" "да"
check "[install] названа причина словами шага" "$(grepq 'ОШИБКА.*(юнит|каталог)' "$OUT")" "да"
check "[install] успеха не рапортует" "$(grepq '(две минуты|2 мин|2 минут)' "$OUT")" "нет"
check "[install] юнитов на диске нет" \
  "$([[ -f "$UNITS/mihomo-refresh.timer" || -f "$UNITS/mihomo-refresh.service" ]] && echo да || echo нет)" "нет"
# Ключевая проверка этой ветки: шаг обязан оборваться НА файловой операции, а не
# доковылять до systemctl. Мутационный прогон 08.09.2026 показал, зачем она нужна:
# со снятыми проверками install шаг все равно падал - но уже на разборе
# расписания, успев включить таймер, юнитов которого на диске нет. Провал по
# коду возврата тут есть в обоих состояниях, и без этой строки тест молчал бы
# одинаково.
check "[install] шаг оборвался до вызова systemctl" "$(grepq '(enable|daemon-reload)' "$LOG")" "нет"

reset rmfail
mkdir -p "$SB/root/etc/cron.d"
printf '*/2 * * * * root /usr/local/sbin/mihomo-refresh\n' > "$CRON"
RC=$(invoke)
check "[rm] код возврата ненулевой" "$([[ "$RC" -ne 0 ]] && echo да || echo нет)" "да"
check "[rm] не утверждает, что cron снят" "$(grepq 'снят прежний' "$OUT")" "нет"
check "[rm] успеха не рапортует" "$(grepq '(две минуты|2 мин|2 минут)' "$OUT")" "нет"
check "[rm] cron.d и правда остался на месте" "$([[ -e "$CRON" ]] && echo есть || echo нет)" "есть"

echo "M. находка 2 сверки: лок держит взаимное исключение и молча пропускает занятое"
reset ok; RC=$(invoke)
check "код возврата" "$RC" "0"
# Форму вызова проверяем на настоящем flock, но в песочнице: боевой лок /run и
# боевую команду подменяем на свои. Проверяется поведение (пропуск без ошибки),
# а не текст строки - текст уже проверен статически в секции C.
EXEC="$(sed -n 's/^ExecStart=//p' "$UNITS/mihomo-refresh.service" | head -n1)"
LOCKF="$SB/refresh.lock"; RAN="$SB/ran.flag"
cat > "$SB/bin/refresh-probe" <<'PROBE'
#!/usr/bin/env bash
printf 'ran\n' >> "$RAN_FLAG"
PROBE
chmod +x "$SB/bin/refresh-probe"
CMD="${EXEC//\/run\/mihomo-refresh.lock/$LOCKF}"
CMD="${CMD//\/usr\/local\/sbin\/mihomo-refresh/$SB/bin/refresh-probe}"

: > "$RAN"
env RAN_FLAG="$RAN" bash -c "$CMD" >/dev/null 2>&1
check "[лок свободен] код возврата" "$?" "0"
check "[лок свободен] прогон состоялся" "$(grepq 'ran' "$RAN")" "да"

: > "$RAN"
exec 9>"$LOCKF"
if flock -n 9; then
  # 9>&- : дочерний процесс не наследует наш дескриптор, иначе он держал бы лок
  # той же записью открытия файла и конфликта бы не увидел.
  env RAN_FLAG="$RAN" bash -c "$CMD" >/dev/null 2>&1 9>&-
  RC=$?
  flock -u 9
  check "[лок занят] выход без ошибки (иначе journal сыплет failed)" "$RC" "0"
  check "[лок занят] прогон НЕ состоялся" "$(grepq 'ran' "$RAN")" "нет"
else
  bad "не удалось взять лок песочницы $LOCKF - сценарий не проверен"
fi
exec 9>&-

echo "N. находка 3 сверки: итог берет расписание из установленного юнита, а не из константы"
# Подставной источник с другим расписанием: если строка успеха - константа, вывод
# не изменится и тест покраснеет. Числа взяты нарочно непохожими на боевые.
mkdir -p "$SB/src/etc/systemd/system"
cp "$ROOT/etc/systemd/system/mihomo-refresh.service" "$SB/src/etc/systemd/system/"
sed -e 's|^OnCalendar=.*|OnCalendar=*:0/7|' -e 's|^RandomizedDelaySec=.*|RandomizedDelaySec=30s|' \
  "$ROOT/etc/systemd/system/mihomo-refresh.timer" > "$SB/src/etc/systemd/system/mihomo-refresh.timer"
reset ok; SRC="$SB/src"; RC=$(invoke); SRC=""
check "код возврата" "$RC" "0"
check "названо расписание из юнита" "$(grepq 'раз в 7 мин' "$OUT")" "да"
check "названа задержка из юнита" "$(grepq '30 с' "$OUT")" "да"
check "боевых чисел в выводе нет" "$(grepq '(2 мин|59 с)' "$OUT")" "нет"

# И обратно: на штатном юните обязана быть ровно подстрока из спеки (уточнение
# 08.09.2026), иначе чтение из юнита сломало бы контракт вывода.
reset ok; RC=$(invoke)
check "код возврата" "$RC" "0"
check "штатный вывод содержит 'раз в 2 мин'" "$(grepq 'раз в 2 мин' "$OUT")" "да"
check "штатный вывод содержит задержку 59 с" "$(grepq '59 с' "$OUT")" "да"

echo "J. песочница: боевая файловая система не тронута"
check "боевой /etc/cron.d/mihomo-refresh на месте" \
  "$([[ -e /etc/cron.d/mihomo-refresh ]] && echo есть || echo нет)" "$REAL_CRON_BEFORE"
check "боевых юнитов не появилось" \
  "$([[ -e /etc/systemd/system/mihomo-refresh.timer || -e /etc/systemd/system/mihomo-refresh.service ]] && echo да || echo нет)" "нет"

echo
[[ $FAILED -eq 0 ]] && echo "ВСЕ СЦЕНАРИИ ПРОШЛИ" || echo "ЕСТЬ ПРОВАЛЫ"
exit $FAILED
