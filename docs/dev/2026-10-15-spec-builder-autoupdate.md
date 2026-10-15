# CASC-AUTOUPDATE: автоматическое обновление сборщика

## Цель и границы

После инцидента Андрея пользователь15октября поручил закрыть проблему автообновления. Решение: включать обновление сборщика по умолчанию на новых установках; существующие подключать вручную. Правила совместимы со старыми клиентами независимо от updater (отдельный CASC-COMPAT). Эта задача обновляет только mihomo-build-config. Ядро mihomo, остальные скрипты, настройки SOCKS/TUN, подписки и локальные правила автоматически не меняются. Платформа прежняя: Linux amd64/arm64 с systemd; поддержка OpenWrt/Entware не заявляется.

## Поведение

Новый Python stdlib CLI /usr/local/sbin/mihomo-update: status, check, update, enable, disable, rollback. Реальные изменения требуют root. Для black-box тестов MIHOMO_UPDATE_ROOT задает sandbox-префикс абсолютных путей; transport/systemctl/mihomo подменяются тестовыми executable в PATH/префиксе, без сети и боевых изменений. Команды возвращают ненулевой код при ошибке; причины называют этап, но не печатают входные конфиги, URLs подписки или сырой вывод builder/core.

Источник кода — main доверенного dewil/mihomo-cascade. Через HTTPS GitHub API разрешается commit SHA (строго40hex), затем скачивается только usr/local/sbin/mihomo-build-config по immutable raw URL этого commit. Никаких скачанных shell-команд, архивов или произвольных download_url из ответа. Размер ответа и время ограничены; ошибка сети/JSON/пустое тело/синтаксис Python оставляют прежнюю установленную версию. Доверие основано на HTTPS и выбранном репозитории; commit SHA фиксирует версию, не является подписью автора. Custom repo/ref установки не должны молча переключаться на main: auto-enable для них не выполняется, требуется явный ручной выбор canonical updater.

При отсутствии учтенного состояния обычные enable/update отказывают с подсказкой --accept-current. Только явные enable --accept-current (с включением timer) или update --accept-current (без включения timer) принимают текущий builder как исходную версию.

Check только показывает доступную ревизию/хеш и не выполняет candidate. Update под /run/mihomo-refresh.lock (ожидание ограничено) скачивает/проверяет candidate, затем копирует нужные входы текущего /etc/mihomo в приватный staging. Обязательны прежние config.yaml и build-state.json при наличии: они сохраняют реальные ограничения orphan/node floor. Удалять MIHOMO_ALLOW_SHRINK из среды проверки; защита не обходится. Candidate выполняется отдельным процессом с MIHOMO_BASE=stage, ограниченным временем; любой ненулевой код, включая3, отвергает обновление. Затем установленный /usr/local/bin/mihomo проверяет staged config через -t -d stage -f stage/config.yaml; отсутствие ядра/ошибка — отказ. Абсолютные и выходящие через .. пути в path-bearing настройках и providers сформированного YAML отвергаются до запуска ядра. При timeout завершается вся группа процессов candidate, включая дочерние, до освобождения lock и удаления staging. Staging не является защитной песочницей от злонамеренного кода доверенного репозитория: код builder выполняется с полномочиями root. Копирование и удаление staging не следует по ссылкам для записи за его пределы; секретные копии/receipt/backup приватны.

После успешных проверок сохранить backup предыдущего builder (права/владелец), SHA256 и запись транзакции в /var/lib/mihomo-updater (0700). Установить candidate атомарной заменой файла в его каталоге. При ошибке установки/фиксации транзакции вернуть предыдущий builder; не сообщать успех при неподтвержденном откате. Незавершенная запись транзакции после сбоя процесса должна позволять консервативное восстановление прежнего builder при следующем update/rollback с проверкой backup hash. State JSON записывается атомарно. Существующие локальные правки builder после последнего учтенного состояния не перетирать молча.

Update не копирует staged config в рабочий каталог, не вызывает refresh, reload, restart или reboot. Только код заменяется; обычный refresh применит его при следующем цикле. Поэтому текущие соединения сохраняются и старый/новый процесс builder видит полный файл через atomic replace. Общий lock исключает updater и штатный timer refresh; прямой ручной builder/startup старого unit не принимает этот lock, но updater не пишет его рабочие входы/выходы. Staging — проверка, а не обещание live-egress. Ошибка будущего refresh при новых данных управляется существующим refresh, не выдается за автоматически проверенный успех updater.

Rollback атомарно возвращает проверенный предыдущий builder, без restart/config изменений. Отклоненный hash сохраняется, чтобы следующий ежедневный update не вернул ту же версию (новый commit с теми же bytes тоже не обходит запрет). Повтор той же установленной версии — no-op, существующий backup не теряется. Status показывает состояние, установленную/доступную проверенную ревизию при наличии, hash, rollback и фактическое состояние timer.

## Установка и включение

Добавить mihomo-update.service (oneshot root, общий lock внутри CLI) и mihomo-update.timer: ежедневно, Persistent=true, RandomizedDelaySec=1h. Не привязывать обновление к каждой загрузке правил. Enable/disable управляют только этим timer, сохраняют выбор и выводят проверяемый результат.

Новый install.sh --step updater из полного локального дерева устанавливает только updater и два его unit, daemon-reload. Не меняет builder/config/старые units/таймер refresh и не рестартует mihomo. Existing клиент выполняет этот шаг из проверенного архива и mihomo-update enable --accept-current один раз; полного install.sh для opt-in не требуется.

Обычная fresh установка скачивает и устанавливает эти файлы и включает timer через enable --accept-current только после успешного запуска mihomo. Действительно новая установка определяется до любых записей: прежних builder/service/updater-state нет. MODE=fresh из-за потерянной subscription.url на старой машине не разрешает принудительное включение. Повторная установка и update сохраняют явное disable. Пользователь выбрал default-on для новых, не глобальное включение на всем парке. Custom repo/ref: не auto-enable canonical updater; явная диагностика.

## Приемка (CASC-AUTOUPDATE)

1. Check не выполняет скачанный код/не меняет builder или конфиги. URL source immutable после проверки SHA, размер/таймауты ограничены; плохой ответ не меняет установку.
2. Хороший candidate + core validation -> атомарная установка только builder; backup сохранен, receipt проверяем. Все live configs/URLs/local rules/state байт-в-байт прежние, нет reload/restart/reboot.
3. Candidate exit1/3, timeout, invalidPython и core absent/reject -> прежние builder и config сохранены; секрет-маркер из stdout/stderr не появляется в выводе CLI. Общий lock исключает запись конкурирующего updater/timer.
4. Ошибка replace/state commit -> автоматический rollback либо явный неподтвержденный rollback; прерванная транзакция восстанавливается. Bad backup hash не исполняется/не устанавливается. Локально измененный builder сохраняется с явным отказом.
5. Manual rollback работает, отклоненная версия повторно не ставится. No-op не уничтожает backup. Enable/disable не трогают mihomo/refresh; status не утверждает то, чего не проверил.
6. Isolated installer: --step updater не меняет существующие конфиги/scripts/units кроме собственных; свежая стандартная установка default-on, existing/повторный disable/custom ref не включает молча. Критическая ошибка установки updater не маскируется.

Нужны независимые RED до реализации, GREEN всех текущих suites и новых, независимая сверка другой моделью. Боевые установки и публикация релиза отдельно по разрешению; live acceptance не подменяется stub-тестами. Автоматического обновления самого updater в этой версии нет.

## Уточнения до реализации

Все изменяющие команды, rollback и восстановление pending берут тот же lock. До первого baseline обычные enable/update отказывают с понятной подсказкой; владелец явно принимает текущий файл командой enable --accept-current (или update --accept-current для ручного обновления без включения timer). Это согласие заменить текущий builder на upstream с сохранением backup, а не утверждение, что файл был штатным. Fresh installer вызывает enable --accept-current только для действительно новой стандартной установки. Локальные изменения после baseline по-прежнему блокируют автоматическую замену.

Timeout candidate/core завершает всю process group и дожидается прекращения дочерних процессов перед удалением staging/отпусканием lock. Пределы для тестов и эксплуатации: MIHOMO_UPDATE_LOCK_TIMEOUT (30 секунд), MIHOMO_UPDATE_COMMAND_TIMEOUT (240 секунд), MIHOMO_UPDATE_FETCH_TIMEOUT (90 секунд); значения строго положительные.

Core получает -d stage -f stage/config.yaml. Копии конфигов приватны, но проверка доверенного root-кода не является security sandbox. Абсолютные/выходящие через .. пути в path-настройках generated config, включая providers и inline YAML maps, должны приводить к отказу обновления: проверка не должна заставлять core писать пользовательские файлы вне staging. Сложные неподдерживаемые формы path-значений отклоняются консервативно; отсутствие новых зависимостей не оправдывает молчаливый обход проверки.

Публичный state schema1: installed={sha256,commit,mode,uid,gid}, previous=такая же запись или null, rejected=[sha256], pending={before:запись,after:запись} или null. Backup immutable по адресу backups/<sha256>.py; его bytes проверяются по hash. Pending записывается до замены builder. После установки installed=after, previous=before, pending=null. Recovery проверяет текущий hash: допустимы только before/after; иные локальные правки сохраняются с отказом. Проверенный before восстанавливается, after попадает в rejected, pending очищается. В этом вызове update новый candidate уже не устанавливается. Check/status pending не исправляют.
