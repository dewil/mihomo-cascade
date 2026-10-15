# Обновление сборщика

`mihomo-update` обновляет только `/usr/local/sbin/mihomo-build-config`.
Работающий mihomo не перезапускается; конфиги, подписки, локальные правила,
ядро и сам updater остаются прежними. Следующий обычный refresh использует
новый builder. Если итоговый конфиг не изменился, refresh не делает reload.

Новые стандартные установки включают timer после успешного запуска mihomo.
Для существующих установок нужно один раз выполнить из проверенного полного
локального дерева проекта:

```bash
sudo bash install.sh --step updater
sudo mihomo-update enable --accept-current
```

Шаг `updater` устанавливает только CLI и два его unit, затем вызывает
`systemctl daemon-reload`. Он не включает timer и не меняет остальные скрипты,
конфиги, unit службы и refresh. Полный `install.sh` для этого не нужен:
он обновляет больше компонентов и перезапускает mihomo.

Первое `--accept-current` принимает установленный builder как baseline. Это
согласие обновлять его из canonical main, а не доказательство его происхождения.
После baseline локальные изменения builder блокируют автоматическую замену,
даже при повторном `--accept-current`. Сначала сохраните и разберите свои изменения.
Custom repo/ref не включают canonical updater автоматически; ручное включение
означает явный выбор `dewil/mihomo-cascade:main`.

## Команды

```bash
sudo mihomo-update status                 # hash, receipt, фактический timer
sudo mihomo-update check                  # скачать/проверить ревизию без исполнения
sudo mihomo-update update                 # проверить и установить builder
sudo mihomo-update update --accept-current # первый ручной update без включения timer
sudo mihomo-update disable                # отключить только updater timer
sudo mihomo-update enable                 # включить его снова
sudo mihomo-update rollback               # вернуть предыдущий builder
journalctl -u mihomo-update.service
systemctl list-timers mihomo-update.timer
```

Timer запускается ежедневно со случайной задержкой до часа, `Persistent=true`.
Отключение не отменяет уже выполняющийся update; изменяющие команды сериализованы
общим lock `/run/mihomo-refresh.lock`. Этот lock также принимает штатный refresh
timer. Старый startup unit и прямой ручной builder его не принимают; updater
поэтому не пишет их рабочие входы и выходы. Установленный код заменяется целиком
атомарно. Для ручного refresh используйте существующий общий lock.

## Проверки и восстановление

Updater получает commit SHA через фиксированный HTTPS GitHub API, затем builder
по immutable raw URL этого SHA. Размер ответа ограничен 1 MiB. Доверие основано
на HTTPS и выбранном репозитории; SHA фиксирует версию, но не является подписью.
Недоступность GitHub сохраняет установленный код и дает ненулевой exit.

Candidate исполняется на приватной копии `/etc/mihomo`, включая прежний config
и build-state. Флаг `MIHOMO_ALLOW_SHRINK` исключен из среды проверки. Любой отказ
builder, включая exit 3, блокирует установку. Затем установленное ядро проверяет
конфиг через `-t -d stage -f stage/config.yaml`. Отсутствие ядра также блокирует
обновление. Таймаут завершает всю process group. Сырой stdout/stderr subprocess
не попадает в вывод updater, поскольку может содержать секреты подписки.

Staging не является security sandbox: доверенный код upstream выполняется с
правами root. Обычные файлы копируются, symlink и специальные файлы отклоняются;
runtime `cache.db` пропускается. Перед запуском ядра проверяются filesystem-пути,
включая provider paths: абсолютные, содержащие `..` и неподдерживаемые сложные
формы отклоняются. В `proxies` поля `path` внутри `ws-opts`, `http-opts`, `h2-opts`
считаются HTTP request paths; обычный `/` в них допустим. Это консервативная
проверка поддерживаемого формата, не полноценный YAML parser.

В `/var/lib/mihomo-updater` (0700) хранятся приватный `state.json` и immutable
backups по SHA256. Перед заменой записывается pending transaction. При следующем
`update` или `rollback` прерванная транзакция восстанавливает проверенный backup
и завершает вызов без новой установки. Неизвестные локальные изменения и
неверный backup hash сохраняют текущий файл с явным отказом.

Rollback не перезапускает процесс и не меняет конфиг. Hash отвергнутой версии
попадает в список rejected: даже новый commit с теми же bytes не установит ее
повторно. No-op update сохраняет предыдущий backup. При ошибке транзакции updater
пытается восстановить предыдущий код; неподтвержденный rollback сообщается
ошибкой, а pending сохраняется для восстановления.

Проверка staging подтверждает сборку и принятие конфигурации установленным ядром,
но не проверяет внешний egress и не гарантирует будущий refresh с другими данными.
Условия ошибок будущего refresh остаются прежними. Автоматического обновления
ядра и updater в этой версии нет. Поддерживаемая платформа — Linux amd64/arm64
с systemd; OpenWrt/Entware не поддерживаются этим механизмом.

## Локальная проверка

```bash
python3 tests/builder-autoupdate.py
bash tests/builder-autoupdate-install.sh
```

Тесты подменяют transport, core и systemctl. Они не выполняют live acceptance.
`MIHOMO_UPDATE_ROOT` задает sandbox-префикс путей; без него изменяющие команды
требуют root. Пределы задаются строго положительными переменными
`MIHOMO_UPDATE_LOCK_TIMEOUT` (30 секунд), `MIHOMO_UPDATE_COMMAND_TIMEOUT` (240)
и `MIHOMO_UPDATE_FETCH_TIMEOUT` (90).
