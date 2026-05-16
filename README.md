# mihomo-cascade

Установка mihomo на сервер для использования в качестве **каскада** (промежуточного звена) при маршрутизации трафика: `hiddify-singbox` / `hiddify-xray` → mihomo SOCKS → внешний прокси по правилам.

## Требования

- Linux (x86_64 или arm64)
- root-доступ
- curl, python3
- systemd

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
- **update** — если `/etc/mihomo/subscription.url` уже непустой. Обновляет только то, что репо считает источником правды: бинарник, `config.base.yaml`, `iso3166_alpha2.txt`, скрипты в `/usr/local/sbin/`, systemd unit, cron. **Не трогает** `subscription.url`, `routing-rules.url`, `routing-rules.yaml`. В конце делает `systemctl restart mihomo`, чтобы новый `mihomo-build-config` подхватился сразу.

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
8. Устанавливает systemd-сервис и cron

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
  providers/vless_sub.yaml         — скачанная подписка
  geoip.metadb                     — GeoIP база
/usr/local/sbin/
  mihomo-build-config              — сборка config.yaml из подписки + правил
  mihomo-refresh                   — пересборка + hot reload без рестарта
  check-route                      — проверка IP напрямую и через туннель
/etc/systemd/system/mihomo.service — systemd unit
/etc/cron.d/mihomo-refresh         — cron: обновление каждую минуту
```

## Как это работает

- При старте сервиса выполняется `mihomo-build-config` (ExecStartPre), который:
  - Скачивает подписку по URL из `subscription.url`
  - Скачивает правила по URL из `routing-rules.url`
  - Генерирует `config.yaml` = `config.base.yaml` + proxy-providers/groups/rules
  - Если IPv6 выключен в ядре сервера, автоматически переключает `tun.auto-route` и `tun.auto-redirect` в `false`, чтобы mihomo не падал на `add rule … address family not supported by protocol`
- Cron каждую минуту запускает `mihomo-refresh` — пересборка конфига + hot reload через REST API.
  - Hot reload (`PUT /configs?force=true`) выполняется только если итоговый `config.yaml` побайтово отличается от предыдущего. Если ни подписка, ни правила не изменились — mihomo не дёргаем.
- TUN-режим: mihomo создаёт интерфейс `tun0`, управляет маршрутизацией автоматически (на ядрах с включённым IPv6 — через `auto-route`, иначе через `mihomo-policy-route` или ручную iptables-разметку).

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
mihomo-refresh                # ручное обновление подписки
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

