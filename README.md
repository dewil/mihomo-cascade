# mihomo-cascade

Установка mihomo на сервер для использования в качестве **каскада** (промежуточного звена) при маршрутизации трафика: `hiddify-singbox` / `hiddify-xray` → mihomo SOCKS → внешний прокси по правилам.

## Требования

- Linux (x86_64 или arm64)
- root-доступ
- curl, python3
- systemd

Про x86_64: официальный релиз mihomo для amd64 требует микроархитектуру **x86-64-v3** (AVX2, BMI2, FMA) и на процессорах ниже этого уровня не стартует вовсе - падает с `This program can only be run on AMD64 processors with v3 microarchitecture support`, а systemd уходит в бесконечный рестарт. `install.sh` определяет уровень сам и на таких машинах ставит сборку `amd64-compatible`. Проверить вручную: `ld.so --help | grep x86-64-v3`.

## Быстрая установка

```bash
chmod +x install.sh
sudo ./install.sh
```

## Установка прямо с GitHub

```bash
wget -qO- https://raw.githubusercontent.com/dewil/mihomo-cascade/main/install.sh | sudo bash
```

Если ветка/путь отличается:

```bash
wget -qO- https://raw.githubusercontent.com/<owner>/<repo>/<branch>/install.sh | \
  sudo GITHUB_REPO="<owner>/<repo>" GITHUB_REF="<branch>" INSTALL_SUBDIR="" bash
```

## Обновление уже развёрнутой ноды

Та же команда, что и для установки, идемпотентна:

```bash
wget -qO- https://raw.githubusercontent.com/dewil/mihomo-cascade/main/install.sh | sudo bash
```

`install.sh` сам определяет режим:

- **fresh** — если `/etc/mihomo/subscription.url` пуст или отсутствует. Запрашивает URL подписки и URL правил маршрутизации интерактивно, заливает все конфиги, запускает mihomo.
- **update** — если `/etc/mihomo/subscription.url` уже непустой. Обновляет только то, что репо считает источником правды: бинарник, `config.base.yaml`, `iso3166_alpha2.txt`, скрипты в `/usr/local/sbin/`, systemd-юниты (`mihomo.service`, `mihomo-refresh.service`, `mihomo-refresh.timer`). Прежний `/etc/cron.d/mihomo-refresh` при этом снимается автоматически - планировщик на ноде остается один. **Не трогает** `subscription.url`, `routing-rules.url`, `routing-rules.yaml`. В конце делает `systemctl restart mihomo`, чтобы новый `mihomo-build-config` подхватился сразу.

Режим можно форсировать переменной `MIHOMO_INSTALL_MODE=fresh|update`.

### Если `raw.githubusercontent.com` недоступен (РФ)

В РФ `raw.githubusercontent.com` периодически режется по SSL handshake — `wget | bash` может молча падать с `OpenSSL SSL_ERROR_SYSCALL` ещё до скачивания скриптов. GitHub Releases (`release-assets.githubusercontent.com`, бинарник mihomo) при этом обычно работает, потому что лежит на Azure-blob CDN.

Обходной путь — rsync исходников с локалки и запуск локального `install.sh` (он сам fallback'ит на локальные файлы, см. `prepare_source_dir`):

```bash
rsync -az --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='*.code-workspace' \
  /path/to/mihomo-cascade/ <host>:/tmp/mihomo-cascade/

ssh <host> 'bash /tmp/mihomo-cascade/install.sh && rm -rf /tmp/mihomo-cascade'
```

## Что делает install.sh

1. Проверяет запуск от root
2. Скачивает бинарник mihomo с GitHub (версия задана переменной `MIHOMO_VERSION` в начале `install.sh`; если уже установлена та же версия — шаг пропускается)
3. Создаёт системного пользователя `mihomo` (с корректным `nologin`)
4. Берёт файлы либо локально, либо скачивает их из GitHub repo
5. Копирует конфигурацию в `/etc/mihomo/`
6. Скачивает GeoIP базу (`geoip.metadb`)
7. Устанавливает скрипты сборки конфига и проверки маршрута в `/usr/local/sbin/`
8. Устанавливает systemd-сервис и таймер обновления, снимает прежний `cron.d`, проверяет, что таймер включен и активен (не так - установка падает с ненулевым кодом)

## Структура

```
/usr/local/bin/mihomo              — бинарник
/etc/mihomo/
  config.base.yaml                 — базовый конфиг (редактировать при необходимости)
  config.yaml                      — автогенерируемый итоговый конфиг
  subscription.url                 — URL подписки
  routing-rules.url                — URL для обновления правил маршрутизации
  routing-rules.yaml               — правила маршрутизации
  iso3166_alpha2.txt               — коды стран для алиасов
  aliases.yaml                     — автогенерируемые алиасы
  build-state.json                 — состояние последней принятой сборки (база для порога по узлам)
  geoip.metadb                     — GeoIP база
/usr/local/sbin/
  mihomo-build-config              — сборка config.yaml из подписки + правил
  mihomo-refresh                   — пересборка + hot reload без рестарта
  mihomo-api                       — чтение Clash API: соединения, группы, задержки
  check-route                      — проверка IP напрямую и через туннель
/etc/systemd/system/
  mihomo.service                   — сам прокси
  mihomo-refresh.service           — разовый прогон обновления (Type=oneshot)
  mihomo-refresh.timer             — расписание: раз в 2 минуты со случайной задержкой
```

## Как это работает

- При старте сервиса выполняется `mihomo-build-config` (ExecStartPre), который:
  - Скачивает подписку по URL из `subscription.url`
  - Скачивает правила по URL из `routing-rules.url`
  - Генерирует `config.yaml` = `config.base.yaml` + узлы статикой (`proxies:`) + группы по алиасам + правила
  - Если IPv6 выключен в ядре сервера, автоматически переключает `tun.auto-route` и `tun.auto-redirect` в `false`, чтобы mihomo не падал на `add rule … address family not supported by protocol`
- `mihomo-refresh.timer` раз в 2 минуты запускает `mihomo-refresh` — пересборка конфига + hot reload через REST API.
  - Расписание задано календарной сеткой (`OnCalendar=*:0/2`) со случайной задержкой `RandomizedDelaySec=59s`. Замер 2026-09-08: агрегатор подписки получал ~900 запросов в час, из них ~346 - шесть машин каскада, бьющих в одну и ту же секунду каждой минуты. Задержка стоит в таймере, а не в скрипте: ручной прогон должен отвечать сразу. `AccuracySec=1s` задан явно - по умолчанию systemd вправе сдвигать запуск в окне до минуты, группируя пробуждения, и эта группировка съела бы джиттер, вернув синхронный залп.
  - Планировщик - systemd, а не cron, потому что systemd на ноде обязателен и так (`mihomo.service` без него не живет), а cron есть не на всякой машине: на ноде без демона крона файл расписания лежал, читать его было некому, и нода не обновлялась молча (поймано 08.09.2026, конфиг был заморожен месяц). Второго пути мы не поддерживаем - при обновлении установщик снимает `/etc/cron.d/mihomo-refresh` сам.
  - Наложение прогонов исключено двумя механизмами сразу. systemd не стартует второй экземпляр `mihomo-refresh.service`, пока активен первый, - но только своего юнита. Поэтому `ExecStart` дополнительно берет `flock -n -E 0 /run/mihomo-refresh.lock` (тот же лок, что брал прежний крон): под него попадает старый cron-прогон в окне миграции - иначе он делил бы с таймером `config.yaml.autobak`, `*.new` и `routing-rules.yaml.tmp`. **Ручной `mihomo-refresh` лока не берет**: он стоит в `ExecStart` юнита, а не внутри скрипта, поэтому запуск руками во время разбора идет параллельно таймеру, как и раньше. `-E 0` означает, что занятый лок - штатный пропуск, а не `failed` в journal. Страховка от зависшего прогона - `TimeoutStartSec=300`.
  - **Проверено на живой ноде 09.09.2026** (`home-server`, первая раскатка таймера): включение и перезапуск таймера немедленного прогона не вызывают - первое срабатывание приходит по расписанию; три срабатывания подряд дали секунды 45 / 21 / 38, то есть джиттер работает; при занятом локе (`flock -n ... sleep 170`) два попавших в окно срабатывания завершились `Result=success` без `failed`, а `build-state.json` не сдвинулся - прогоны честно пропущены, а не выполнены поверх чужого; после освобождения лока ближайшее срабатывание отработало и сдвинуло состояние. До этого все три свойства были выведены из документации systemd, но не наблюдались.
  - **Раскатано на весь парк 09.09.2026** режимом `install.sh --step scheduler` (полный прогон не годится: он заканчивается `systemctl restart mihomo` и рвет живые соединения). Шаг конфига не касается - на всех шести машинах `config.yaml` не изменился ни по md5, ни по mtime, `NRestarts` у `mihomo` не вырос. Соединения на машинах под нагрузкой: llm 16 -> 15, ru3 57 -> 57, s79 6 -> 6, ru 147 -> 158, ru2 227 -> 216 - то есть обычные колебания, а не разрыв. **Синхронный залп исчез**: в одном тике машины сработали на 04, 05, 28, 29 и 49 секунде.
  - На ноде с `systemd-cron` (генерирует юниты из `/etc/cron.d`) снятие файла подхватилось само - `cron-update.path` убрал сгенерированный `cron-mihomo-refresh-root-0.service`, ручного вмешательства не потребовалось. Полагаться на это не стоит: это свойство конкретного генератора, а не гарантия, поэтому установщику нужна проверка "юнита с таким именем в системе больше нет" (таска в бэклоге).
  - Hot reload (`PUT /configs?force=true`) выполняется только если итоговый `config.yaml` побайтово отличается от предыдущего. Если ни подписка, ни правила не изменились — mihomo не дёргаем.
  - Перед подгрузкой конфиг проверяется самим бинарём (`mihomo -t`). Не прошёл — откатываемся на предыдущую копию (`config.yaml.autobak`) и пишем строку в journal, в работе остаётся то, что работало.
  - `PUT` не прошёл — смотрим, жив ли API. **Оборванный ответ при живом API считается применением**: перезагружаясь, mihomo поднимает листенеры заново и рвёт само API-соединение (`curl` видит `Empty reply from server`), а конфиг при этом уже применён. Слепой откат в такой ситуации разводил диск и рантайм, и следующий прогон слал `PUT` снова — reload каждую минуту. API не отвечает — тогда откат: иначе на диске лежала бы версия, которой нет в работе, и следующий прогон, увидев совпадение хэшей, не повторил бы попытку.
- TUN-режим: mihomo создаёт интерфейс `tun0`, управляет маршрутизацией автоматически (на ядрах с включённым IPv6 — через `auto-route`, иначе через `mihomo-policy-route` или ручную iptables-разметку).

### Защита от усохшего списка узлов

`mihomo-build-config` не применяет подписку, в которой узлов заметно меньше, чем в прошлый раз: обычно это значит, что агрегатор не достучался до части нод, а не то, что ноды исчезли. Порог — доля от прошлого состава (`MIHOMO_MIN_NODES_RATIO`, по умолчанию `0.7`) и абсолютный минимум (`MIHOMO_MIN_NODES_ABS`, по умолчанию `3`). Сокращение в пределах порога применяется, но пишется предупреждение.

Сработал порог — сборка отменяется (код `3`), `config.yaml` остаётся прежним, `mihomo-refresh` завершается штатно и пишет предупреждение в journal.

### Политика из правил, оставшаяся без узла

Второй повод для кода `3`, и он про другую беду. Если правило ссылается на политику (`de`, `ee`, …), а подходящего узла в подписке нет, раньше собиралась группа-заглушка с `DIRECT` — и трафик, которому положено идти через выход, уходил прямым адресом ноды. Молча, пока узел не вернётся: снаружи это выглядело как случайные `403` от сервисов, отбивающих запросы из дата-центров.

Порог по составу от этого не защищает: он ловит обвал списка, а потеря одного узла из двенадцати проходит `0.7` беспрепятственно.

Развилка не «`DIRECT` или `REJECT`», а «есть ли что оставить в работе»:

- **предыдущий `config.yaml` есть** — сборка отменяется (код `3`), в работе остаётся конфиг, собранный когда узлы были на месте. Одиночный промах агрегатора не роняет сервис;
- **предыдущего конфига нет** (первая установка, сборка после отката) — отменять нечего, собирается заглушка с `REJECT`. Трафик отклоняется заметно, но прямым выходом не уходит **ни в одной ветке**.

Алиасы, на которые правила не ссылаются, сборку не блокируют: группа, которую никто не упоминает, — не повод останавливать ноду.

`MIHOMO_ALLOW_SHRINK=1` снимает и этот отказ тоже, собирая с `REJECT` вместо отмены. **У флага два смысла**, и включающий должен знать оба: при плановом выводе ноды её надо убрать и из правил, иначе политика без узла даст `REJECT`.

Проверяется сценариями в `tests/stub-policy.sh` — песочница через `MIHOMO_BASE`, без root и без боевого `/etc/mihomo`. База для сравнения — `build-state.json` (состояние последней принятой сборки), а не побочный артефакт на диске. Если ноду вывели специально и список должен сократиться:

```bash
MIHOMO_ALLOW_SHRINK=1 mihomo-refresh
```

### Что видно при поломке

Провалы и предупреждения сборщика уходят в journal с тегом `mihomo-refresh` (свой вывод юнит пишет туда же, но тег делает записи сборщика отдельно находимыми). `mihomo-refresh` перехватывает вывод `mihomo-build-config` и кладёт его туда же — иначе причина отмены видна только тому, кто запускал руками.

Повтор одинакового текста глушится: сборщик печатает свои замечания на **каждом** прогоне, и без этого одна строка давала бы сотни записей в сутки, топя в себе первое настоящее событие. Пишем при смене текста или раз в `MIHOMO_LOG_REPEAT_SEC` (по умолчанию 1800 с). Обратная сторона: **журнал недосчитывает повторы** — для замера частоты берите снимок состава конфига, а не число строк.

```bash
journalctl -t mihomo-refresh --since '24 hours ago'   # что сказал сам сборщик
journalctl -u mihomo-refresh --since '24 hours ago'   # прогоны глазами systemd
```

Планировщик проверяется отдельно от прогонов - таймер живет своей жизнью:

```bash
systemctl status mihomo-refresh.timer   # включен ли, когда следующий запуск
systemctl list-timers mihomo-refresh.timer
```

Пустой `list-timers` при установленном каскаде значит, что обновления нет вовсе: до 08.09.2026 такое состояние установщик не замечал и рапортовал успех.

**Откат на cron, если таймер оказался хуже.** Файла расписания в репозитории больше нет, поэтому его нужно достать из истории:

```bash
systemctl disable --now mihomo-refresh.timer
rm -f /etc/systemd/system/mihomo-refresh.{service,timer}
systemctl daemon-reload
git show 8664bd7:etc/cron.d/mihomo-refresh > /etc/cron.d/mihomo-refresh   # из клона репозитория
chmod 644 /etc/cron.d/mihomo-refresh
systemctl is-active cron || systemctl is-active crond                     # без демона откат бесполезен
```

Две вещи, о которых узнают в неподходящий момент. На машине **без демона крона** откат невозможен в принципе - именно этим она и отличалась (`home-server`). И **после отката нельзя запускать `install.sh`**: он снимет `cron.d` и вернет таймер, то есть откат отменит сам себя при ближайшем обновлении ноды.

### Узлы инлайнятся в конфиг, а не приезжают провайдером

До 2026-08-05 узлы жили в `providers/vless_sub.yaml`, а `config.yaml` ссылался на него через `proxy-providers: type: file`. Оказалось, что живые соединения рвёт **именно перечитывание провайдера** — не reload сам по себе и не флаг `force`. Замеры на ru3 (1.19.25, изолированные стенды со своими каталогами и портами, один и тот же узел, поток 8 МБ, reload на 10-й секунде):

| стенд | порвано |
| --- | --- |
| узлы статикой в конфиге | 0 из 8 |
| тот же узел через `proxy-providers: type: file` | 4 из 8 |
| копия боевого конфига на провайдерах (11 шт., 556 правил) | 3 из 10, `plain` и `force` вперемешку |
| **боевой конфиг после перехода на инлайн** | **0 из 8** |
| контроль без reload | 0 из 6 |

Разрывы вероятностные (~40% на провайдерной схеме), поэтому проверять это единичным прогоном бессмысленно — нужна серия. Разницы между `PUT /configs` и `PUT /configs?force=true` в данных нет; `force=true` оставлен, потому что без него не применяются правки базовой части (порты, `listeners:`).

Провайдер был лишь способом доставки — разобранные узлы у билдера и так на руках. Группы теперь `url-test` с одним участником: автопереключать не на что, но health-check сохраняется (раньше его давал провайдер, на нём держится замер задержки в `mihomo-api groups`).

## Настройка под новый сервер

Перед запуском можно отредактировать:

- `etc/mihomo/subscription.url` — URL подписки (при необходимости изменить)
- `etc/mihomo/config.base.yaml` — порты, DNS, исключения из маршрутизации
- `etc/mihomo/routing-rules.yaml` — правила маршрутизации трафика

Для запуска через `wget ... | bash` можно передать:

- `GITHUB_REPO` — `owner/repo`
- `GITHUB_REF` — ветка или тег (по умолчанию `main`)
- `INSTALL_SUBDIR` — подпапка установки в репозитории (по умолчанию пусто, корень репо)

## Проверка маршрута

После установки доступна команда `check-route` для быстрой проверки маршрутизации:

```bash
check-route
```

Скрипт выводит:

- `local ip` — обычный внешний IP сервера
- `tunnel ip` — IP через маршрутизацию mihomo (по доменам `ipv4.icanhazip.com` и `api.ipify.org`)

## Управление

```bash
systemctl status mihomo       # статус
systemctl restart mihomo      # перезапуск
journalctl -u mihomo -f       # логи
mihomo-refresh                # ручное обновление подписки (сразу, без задержки таймера)
systemctl status mihomo-refresh.timer   # планировщик обновления: включен, следующий запуск
journalctl -u mihomo-refresh -f         # прогоны обновления
mihomo-api conns              # живые соединения: хост, правило, цепочка, трафик
mihomo-api conns instagram    # то же с фильтром по подстроке
mihomo-api groups             # группы и выбранный в каждой узел с задержкой
mihomo-api delay de2          # замер задержки узла или группы
check-route                   # проверка маршрутов
```

## Каскад поверх Hiddify

Цель — направить исходящий трафик `hiddify-singbox` и `hiddify-xray` через локальный mihomo SOCKS (`127.0.0.1:7890`), чтобы mihomo по своим правилам распределял трафик между внешними провайдерами из подписки.

### Бэкап

```bash
cp /opt/hiddify-manager/singbox/configs/06_outbounds.json{,.bak}
cp /opt/hiddify-manager/singbox/configs/06_outbounds.json.j2{,.bak}
cp /opt/hiddify-manager/xray/configs/06_outbounds.json{,.bak}
cp /opt/hiddify-manager/xray/configs/06_outbounds.json.j2{,.bak}
```

### singbox

В `/opt/hiddify-manager/singbox/configs/06_outbounds.json` шапка `outbounds`:

```json
{
  "outbounds": [
    { "tag": "freedom", "type": "socks", "version": "5", "server": "127.0.0.1", "server_port": 7890, "udp_over_tcp": false },
    { "tag": "mihomo",  "type": "socks", "version": "5", "server": "127.0.0.1", "server_port": 7890, "udp_over_tcp": false },
    { "tag": "direct",  "type": "direct" },
    { "tag": "WARP",    "type": "direct", "bind_interface": "warp" }
  ]
}
```

### xray

В `/opt/hiddify-manager/xray/configs/06_outbounds.json` шапка `outbounds`:

```json
{
  "outbounds": [
    { "tag": "freedom", "protocol": "socks", "settings": { "servers": [ { "address": "127.0.0.1", "port": 7890 } ] } },
    { "tag": "mihomo",  "protocol": "socks", "settings": { "servers": [ { "address": "127.0.0.1", "port": 7890 } ] } },
    { "tag": "direct",  "protocol": "freedom", "domainStrategy": "UseIPv4v6", "settings": {} },
    { "tag": "WARP",    "protocol": "freedom", "domainStrategy": "UseIPv4v6", "streamSettings": { "sockopt": { "tcpFastOpen": true, "interface": "warp" } } },
    { "tag": "blackhole", "protocol": "blackhole" },
    { "tag": "forbidden_sites", "protocol": "blackhole" },
    { "tag": "DNS-Internal", "protocol": "dns", "settings": { "port": 53 } }
  ]
}
```

### ⚠️ Парная правка `.j2`

`hiddify-panel` периодически рендерит `*.json.j2 → *.json`. Если поправить только `.json`, при следующем рендере правка затрётся. **Обязательно** правьте обе пары:

- `06_outbounds.json` + `06_outbounds.json.j2` (singbox)
- `06_outbounds.json` + `06_outbounds.json.j2` (xray)

В `.j2` замените исходный `freedom`-блок на `socks`-блок к `127.0.0.1:7890` (и добавьте рядом `mihomo` outbound). Jinja-разметка тут не нужна.

### Перезапуск и проверка

```bash
systemctl restart hiddify-singbox hiddify-xray
sleep 3
systemctl is-active hiddify-singbox hiddify-xray
```

Базовая проверка — три curl'а должны давать **одинаковые** IP (выход mihomo), а не реальный IP сервера:

```bash
curl -s --max-time 8  https://api.ipify.org                                   # 1. реальный IP сервера
curl -s --max-time 12 --socks5-hostname 127.0.0.1:7890 https://api.ipify.org  # 2. через mihomo напрямую
curl -s --max-time 12 --socks5-hostname 127.0.0.1:2000 https://api.ipify.org  # 3. через singbox → mihomo
```

Ожидается: (2) и (3) — одинаковые и **отличаются** от (1). Если совпадают с (1) — цепочка не собралась.

