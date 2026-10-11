# Consul health pilot

Изолированный Python 3.11 stdlib пилот. Не меняет defaults, подписки, маршруты или production alerts. Лаборатория использует настоящий Consul: один server, четыре client agents, TTL checks и heartbeat checks, consistent Health API, отдельные scoped writer/evaluator ACL tokens, default deny, script checks disabled. Все listeners loopback.

Нужен заранее предоставленный Consul **2.0.4 linux amd64**, SHA256 бинарника `9f80affeb492d5e2d8d6c8626107666923e1935ec41c1a3fc6ebcce8846368ec`. Runner проверяет hash до запуска; не скачивает и не устанавливает зависимости. Linux `/proc` и `pidfd` обязательны.

```bash
python3 pilot/consul-health/consul_pilot.py evaluate --input /tmp/snapshot.json --now-ms 1791700000000 --max-age-ms 4000
python3 pilot/consul-health/consul_pilot.py run_lab --consul-bin /absolute/path/consul --runtime-dir /tmp/consul-private-new --output-dir /tmp/consul-report
python3 pilot/consul-health/consul_pilot.py run_dev --consul-bin /absolute/path/consul --runtime-dir /tmp/consul-dev-private-new --output-dir /tmp/consul-dev-report --target-host de4.cactushub.app --dev-node-id 42
```

`run_dev` без `--allow-dev-faults` выполняет preflight/baseline: `acceptance_complete=false`. Fault флаг разрешает только bounded dev-сценарии после guards/watchdog. Production fault/config changes запрещены. Dev требует существующие SSH aliases/known_hosts, dev node42/probe profile, temporary mihomo и read-only panel API; подробности в принятой спецификации.

Runtime — новый/пустой собственный каталог под `/tmp`, не symlink, 0700. Config/token/log файлы 0600, никогда не копируются в отчет. Не передавайте runtime внутри git, синка или output. На success/error cleanup завершает только собственные процессы и удаляет только созданные runtime файлы. Независимый detached guardian удерживает pidfds, завершает Consul при потере runner или deadline1200s. Он не сигналит по имени процесса и не затрагивает PID reuse. При аварийном SIGKILL runner guardian завершает процессы и удаляет известные собственные runtime пути; неизвестные имена не удаляются. Readiness-файл в private runtime содержит только PID/endpoint/path для независимого локального чтения; токен находится в отдельном protected файле.

Stdout CLI — один sanitized JSON object. Exit0: evaluate либо принятый requested run; exit1: сценарий не принят; exit2: contract/dependency/preflight. Duplicate JSON keys, unknown fields/flags, invalid state/reason и input >1MiB отклоняются. Все expected cells существуют даже при пустом catalog. Missing/stale/future heartbeat/report, TTL expiration и потеря control plane дают unknown. TTL critical не превращается в свежий target fail.

Lab выполняет baseline≥30s и по три fault/recovery цикла: single path, row, target column, partition, primary-control-only, service listener down, accounting clock freeze, observer agent loss и control plane loss. HTTP policy faults — **synthetic fixture**, четыре AS/DC/domain labels вымышлены на одном хосте. Каждый result проходит actual Consul register/update/read. Application объединяет `/healthz` и `/payload`; single-URL comparator выполняет HEAD без redirects и принимает любой HTTP status, как pinned Mihomo v1.19.25 URLTest. Consul check_update_interval=1s установлен явно: default задерживает публикацию изменившегося Output при неизменном status. Интервал1s/timeout0.5s ускорены относительно production30s/5s; измерения не являются production latency. Bound15s не расширяется при неуспехе.

Output: `report.md`, `evidence.json`, включая events, timestamps, latency, comparator samples, API counts, cleanup, RSS/CPU/disk и source hashes. Export events — чистый прототип; production_delivery=false, future consumer обязан сохранять dedup/last definitive state. Лаборатория доказывает работу Consul/API/модели; реальный VLESS, Hiddify accounting и recovery доказываются отдельно `run_dev`. Родитель CONSUL-2 до dev acceptance не закрывается.

```bash
python3 -m unittest discover -s pilot/consul-health/tests -p test_consul_pilot.py
CONSUL_BIN=/absolute/path/consul python3 -m unittest discover -s pilot/consul-health/tests
```

Спецификация: [CONSUL-2](../../docs/done/2026-10-11-spec-consul-pilot.md). Итоги: [LAB и DEV](../../docs/done/2026-10-11-consul-pilot-report.md). Закрепленные blind tests не изменяются implementation.

История implementation validation: первый real run не прошел baseline из-за отложенной синхронизации Output при default check_update_interval. Второй завершил baseline и27 fault rows, затем честно завершился CONSUL_API при остановке server: writer ACL resolution зависит от server. Оба failed; не считаются acceptance. Исправления: explicit1s sync и отсутствие ложного утверждения успешной публикации во время cp outage; actual consistent read возвращает unavailable.

Dev freshness baseline: после положительной дельты учета непосредственно перед каждым отказом вновь проверяются прямые пробы и пять необходимых Consul cells. Дополнительные application/accounting ячейки удаленных наблюдателей намеренно unknown. Отчеты и heartbeat должны быть обновлены после начала этой baseline-проверки. 15 секунд — предел принятия baseline: поздно завершившийся sample отклоняется до инъекции; это не асинхронная отмена коллектора. Read-only сбор может ждать индивидуальный adapter timeout до45 секунд. Живой observer-loss длится25 секунд при freshness15 секунд; лабораторный detection/recovery bound15 секунд не меняется.

`run_dev` привязан к текущему стенду: использует существующий `/data/git/cactus-adm-geo-as-count/tools/line-probe/line-probe.py` и `/usr/local/bin/mihomo`1.19.25. Зависимость проверяется до запуска; это не переносимый production collector. После исправления отчетной метрики unknown_duration_ms означает sample-and-hold для пяти проверяемых cells; unknown_intervals отдельно показывает полную матрицу, gaps и метод. Исходные live-артефакты и последующий derived audit разведены в отчете.
