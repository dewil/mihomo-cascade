# CONSUL-2: dev-пилот распределенных проверок

Статус: ACCEPTED, 11.10.2026. Владелец приемки: основной агент / dwl. Этот файл не разрешает исполнение fault-сценариев: основной агент сначала проверяет и фиксирует конкретный план восстановления. Контракт заморожен основным агентом; следующим этапом независимый blind RED.

## Замысел и границы

Проверяем, дает ли настоящий Consul полезное преимущество перед текущей одной URL-пробой Cactus: распределенная регистрация состояний, TTL, получение через API, различение потери наблюдателя и отказа проверяемого пути. Диагностическая логика остается нашей. Gossip не заменяет прикладную пробу. Результат — измерения, прототип контракта истории/инцидентов и решение integrate/limit/reject с затратами и откатом.

Две обязательные части: лаборатория с реальными Consul subprocess/API и контролируемыми fixture-отказами; живые ограниченные проверки существующей dev Hiddify de4. Лаборатория не заменяет проверку Hiddify, учета и VLESS. Production не меняется, ноды не исключаются из подписки, сообщения/алерты не отправляются. Автоматическое применение verdict в Mihomo не входит.

Код только в `pilot/consul-health/`; нет изменений установки/defaults. Python 3.11 stdlib, отдельно предоставленный закрепленный Consul binary. Нет автоматической установки/скачивания, новых system services, firewall/SSH/auth changes. Лабораторный runtime вне git и синка: каталог 0700, файлы секретов/конфигов/логов 0600. Consul listeners только loopback; ACL default deny, separate observer write/read evaluator tokens, script checks выключены. Секреты не попадают в argv, stdout, Consul Output, event export или review artifacts. Ошибки выдаются кодами, без сырых исключений/ответов.

## Публичный контракт

Модуль `pilot/consul-health/consul_pilot.py` предоставляет:

```python
evaluate(snapshot: dict, *, now_ms: int, max_age_ms: int) -> dict
run_lab(*, consul_binary: str, runtime_dir: str, output_dir: str) -> dict
run_dev(*, consul_binary: str, runtime_dir: str, output_dir: str,
        target_host: str, dev_node_id: int, allow_dev_faults: bool = False) -> dict
```

```text
python3 pilot/consul-health/consul_pilot.py evaluate --input FILE --now-ms INT --max-age-ms INT
python3 pilot/consul-health/consul_pilot.py run_lab --consul-bin ABS --runtime-dir ABS --output-dir ABS
python3 pilot/consul-health/consul_pilot.py run_dev --consul-bin ABS --runtime-dir ABS --output-dir ABS --target-host de4.cactushub.app --dev-node-id 42 [--allow-dev-faults]
```

CLI stdout — один sanitized JSON object. Exit 0: evaluate выполнен (unknown допустим), либо весь запрошенный run принят; 1: сценарий/приемка не пройдены; 2: неверный контракт/зависимость/preflight. `run_dev` без флага делает только preflight/baseline и возвращает `acceptance_complete=false`, не изображает полный прогон. Неизвестные subcommand/flags — ошибка. Python evaluate при структурно неверном вводе бросает ValueError с постоянным кодом без входного значения.

README будущей реализации описывает эти команды, dependency/version, protected runtime, cleanup и обе границы доказательства. Одного небольшого runner и evaluator достаточно; новый универсальный framework не нужен.

### Snapshot v1

Все поля обязательны; отсутствующие поля не получают оптимистичный default. Время — integer UTC epoch milliseconds, не bool. Числа finite; now_ms >= 0, max_age_ms > 0. IDs — уникальные непустые ASCII `[A-Za-z0-9_-]+`. Неизвестные поля отклоняются, чтобы избежать молчаливого дрейфа schema.

```text
{
 schema_version: 1,
 control_plane: "ok"|"unavailable"|"acl_denied"|"partial",
 topology_kind: "synthetic_single_host"|"real",
 expected_checks: ["transport", "application", "accounting"],
 observers: [{id, asn: string|null, dc: string|null,
              physical_domain: string|null, last_seen_ms: integer|null}],
 targets: [{id}],
 observations: [{observer, target, check, observed_at_ms: integer,
                 seq: integer>=0, state: "pass"|"fail"|"unknown", reason}]
}
```

`expected_checks` — непустое подмножество трех типов без повторов; порядок семантически несущественен. observations обязаны ссылаться на известные observer/target/check. Полная ожидаемая матрица — observers × targets × expected_checks. Пустые observers/targets недопустимы. Для лаборатории можно применять observers/targets с одинаковыми IDs, это разные пространства; self-pairs также ожидаются.

Входные reasons: pass только `ok`; fail — `connect_failed`, `timeout`, `http_status`, `body_mismatch`, `accounting_stale`; unknown — `probe_error`, `missing`, `invalid_report`. Сырое исключение/URL/body не reason. Противоречивые state/reason или неверный тип — structural error. Валидный future timestamp допускается schema, но становится unknown при оценке.

### Нормализация и приоритеты

INV-CONSUL-01: отсутствие свежего доказательства не является отрицательной пробой target. Каждая ожидаемая cell существует в output даже при пустом catalog.

1. При control_plane != ok все cells unknown с `control_plane_unavailable`, `acl_denied` или `partial_catalog` соответственно. Нельзя выдавать last-known за текущее здоровье.
2. При last_seen_ms=null observer cells unknown `observer_missing`; future heartbeat — `observer_clock_skew`; возраст > max_age_ms — `observer_stale`.
3. Для каждой cell выбирается наибольший seq. Идентичные записи с этим seq схлопываются. Различающиеся timestamp/state/reason с максимальным seq дают unknown `sequence_conflict`; младшие seq игнорируются. Нет записи — unknown `missing`.
4. Выбранная future observation дает unknown `clock_skew`; возраст > max_age_ms — unknown `stale`; равенство порогу считается свежим. Протухший старший seq не заменяется младшим свежим pass.
5. Иначе сохраняются state/reason выбранной записи. Ни transport pass, ни observer heartbeat не подразумевают application/accounting pass.

В Consul просрочка TTL может стать critical, но без свежего собственного report это unknown. Collector не создает свежий fail из текстового сообщения об истечении TTL. ACL denial, заголовок `X-Consul-Results-Filtered-By-ACLs: true`, некорректный catalog response дают соответствующий cp status, не «все здоровы». Отдельно отсутствующий ожидаемый check при успешном полном ответе дает missing. Collector сохраняет собственные seq/timestamp; время чтения API не освежает старую пробу.

### Детерминированный output v1

```text
{
 schema_version: 1, control_plane, topology_kind, evaluated_at_ms: now_ms,
 cells: [{observer, target, check, state, reason,
          observed_at_ms: integer|null, seq: integer|null}],
 witness_groups: [[observer_id, ...], ...],
 patterns: [{kind, check, observers: [id,...], targets: [id,...],
             evidence: ["observer/target/check",...],
             proof_kind: "synthetic"|"observed",
             independent_groups: integer}],
 fatal_cause: null
}
```

Cells сортируются по `(observer,target,check)` лексикографически. При причинах из шагов 1–2 и missing metadata seq/timestamp=null; sequence_conflict также null; при stale/clock_skew сохраняется metadata выбранной записи. Witness IDs внутри групп и группы лексикографически отсортированы. Массивы pattern IDs/evidence уникальны и отсортированы; patterns сортируются по `(kind,check,joined_observers,joined_targets)`. Порядок input не влияет на output.

INV-CONSUL-02: общие AS **или** DC **или** physical_domain связывают observers; независимые группы — компоненты связности этого графа. Observer с любым неизвестным полем conservatively связан со всеми. Для `real` один общий physical_domain всегда означает один голос. Для synthetic topology используются явно заданные вымышленные AS/DC/physical domains только для проверки группировки; output proof_kind=synthetic и отчет явно указывает один настоящий хост. Это не свидетельство физической независимости.

INV-CONSUL-03: patterns описывают наблюдения, не доказывают причину. По таймаутам нельзя объявлять падение ДЦ или глобальную недоступность. Классификация отдельно для каждого check; unknown не голосует и не считается pass.

- `single_path`: ровно один fail во всей матрице check; у того же observer есть pass к другому target, у другого observer есть pass к failed target. Evidence включает fail и все такие подтверждающие pass.
- `observer_row`: observer имеет fail к >=2 targets; каждый из этих targets имеет pass от другого observer. Pattern содержит одного observer и все его failed targets; evidence включает его fail и подтверждающие pass.
- `target_column`: target имеет fail от >=2 witness groups; каждый голосующий observer имеет pass к какому-либо другому target. Pattern содержит всех подходящих failed observers; evidence включает fail и их контрольные pass. Количество групп считается только среди подходящих observers.
- `partition_candidate`: только для одинаковых множеств observer/target IDs; имеется разбиение всех IDs на два непустых блока, внутри блоков все cells pass, между блоками в обоих направлениях все fail. Найти каноническое разбиение (минимальный ID в первом блоке; алгоритм не обязан перебирать экспоненциальное число вариантов), выдать один pattern с обоими полными множествами IDs, evidence — все cells; группа/причина аварии не утверждается.
- `insufficient_evidence`: один pattern на check, если есть fail или unknown, но ни одно правило выше не сработало; evidence — все non-pass cells. Если все pass, patterns для check пусты.

Patterns могут сосуществовать; нет приоритетного затирaния row колонкой. Для unknown control plane patterns=[] (причина полностью представлена cells), witness_groups все равно вычисляются. independent_groups — количество witness groups, пересекающих observers pattern; для insufficient_evidence это все observers с non-pass cells.

## Лаборатория: настоящий Consul, ограниченные fixtures

REQ-CONSUL-01: один server + четыре client agents, отдельные subprocess/data-dir/ports на loopback; четыре observer и четыре target fixtures. Используется закрепленный binary, версия и hash отражены в evidence. Consul KV/dict не заменяет checks. Проверить register/update TTL через `/v1/agent/check/register`, `/v1/agent/check/update/:id`, чтение `/v1/health/state/any?consistent` и настоящее истечение TTL. Есть отдельные heartbeat TTL checks. ACL токены читателя и писателей разделены; anonymous write и reader write должны быть отклонены.

Targets отвечают `/healthz` фиксированным body, `/accounting` возрастом обновления. Transport — отдельный TCP connect. Application — HTTP непосредственно в выбранный target без fallback/redirect. Accounting fixture проверяет именно возраст. Pair policies могут возвращать ошибку/задержку конкретному observer: это контролируемый synthetic path fault, не реальный network partition. Все probe results проходят настоящий Consul API до evaluator.

REQ-CONSUL-02: healthy baseline >=30 с; service_down, worker_hang, observer_lost, client_path_bad, row_fault, target_column, partition, control_plane_unavailable и recovery, по три повторения каждого лабораторного сценария. Интервал 1 с, timeout 0,5 с, freshness 4 с, TTL 5 с. Freshness оценивает момент завершения пробы. У каждого repeat измеряются injection/removal/detection/recovery timestamps, latency, false target-down, unknown duration. Предел обнаружения/восстановления 15 с; превышение — failed criterion, не расширение порога постфактум.

REQ-CONSUL-03: runner owns только собственные PID/process groups/runtime files. На success/error/interrupt cleanup завершает их, не чужие процессы. Не использовать kill по общему имени Consul. Все sockets loopback; никаких установленных служб или открытых внешних API.

## Живая dev-фаза: Hiddify, клиентский путь и учет

Read-only discovery 11.10: dev DB содержит единственную active ноду42 de4.cactushub.app, prod DB записей этого домена не содержит; existing dev is_probe user один. Штатный Plan::build вернул один VLESS TLS endpoint42 на443. Root SSH alias de4-cactus работает; восемь Hiddify services active. `ss` подтвердил public443 owner haproxy IPv4/IPv6. Эти факты перед fault перепроверяются, discovery не заменяет preflight.

REQ-CONSUL-04: run_dev принимает только exact hostname `de4.cactushub.app` и id42, требует fresh remote evidence prodrows=0, dev node identity/active, existing probe profile только для42, текущие active baseline всех восьми служб и реальный public443 owner. Любое несоответствие/неполный ответ прекращает фазу до первого изменения. Никаких новых пользователей, редактирования профиля/DB, sync/prune или production control. Обязательный baseline TCP + actual VLESS/TLS HTTP через временный клиент, закрепленный за de4 без fallback, и read-only panel API GET с проверкой ожидаемой структуры. Клиент использует отдельный listener/config; существующие mihomo defaults не трогаются.

Профиль берется через штатный `App\Services\Monitoring\LineProbe\Plan` dev приложения на s79, PHP `/opt/php85/bin/php`. SSH subprocess capture держит секретный ответ в RAM и не передает его в tool stdout. Конфиг клиента — protected file0600, directory0700 вне синка; очищается. В Consul/отчете только typed state/reason/duration/node42. SSH/server/API exception не печатать сырыми. Нельзя выводить URL подписки, URI, UUID, api_key, private keys. Секреты не передаются независимому reviewer/test-writer.

REQ-CONSUL-05: local service, panel/application, client path и accounting — отдельные сигналы. Worker-success timestamp из bounded tail `hiddify_panel_background_tasks.err.log` доказывает только liveness. Проверенный source: task каждые60с, worker pool=solo/concurrency1; `user_driver.get_users_usage()` ловит exceptions отдельных drivers и продолжает. Поэтому даже Celery `succeeded` может маскировать отказ учета. Redis lock также обновляется в except и не является health success.

Accounting acceptance требует: существующий выделенный dev is_probe user; read-only получение его current_usage до/после ограниченного реального трафика через выбранный VLESS; счетчик действительно увеличился в baseline и после recovery. Наружу выходят только `counter_advanced`, `delta_bytes`, `sample_age_ms`, без user identity. Не читать reset=True usage API: это изменяет счетчики. При reset/decrease/невалидных единицах/отсутствии безопасного read-only counter adapter результат unknown и критерий остается открытым. Во время STOP worker контролируемый трафик продолжается; после времени на завершение уже начатого accounting cycle счетчик не продвигается, success age становится >150с, хотя panel/client остаются доступны. После CONT задача и счетчик снова продвигаются. Измеряется задержка, исходный threshold150с явно отличается от лабораторных4с.

REQ-CONSUL-06: один повтор каждого живого сценария достаточен; ограничение и отсутствие статистической значимости указываются. Fault plan заранее проверяет основной агент, а run_dev дополнительно требует allow_dev_faults.

1. `service_down`: после повторной проверки ownership public443 краткий stop `hiddify-haproxy` только de4, максимум30с; independent watchdog start восстанавливает его не позднее45с. Сравнить service state, panel public URL и actual client path. Восстановление проверяется новыми пробами.
2. `worker_hang`: STOP только проверенному процессу dev background-tasks (включая релевантные worker children); лимит180с, independent watchdog CONT/start не позднее195с. Panel и VLESS должны остаться живыми. Перед STOP зафиксированы unit/PID/start identity и положительный accounting baseline. Параллельный observer не должен быть заморожен вместе с worker.
3. `client_path_bad`: остановить/заморозить собственный user-space relay экспериментального VLESS клиента; независимая прямая проба de4 и ее службы сохраняют здоровье. Это реальный отказ искусственно созданного клиентского участка, не географическая/DPI авария. Нельзя заменять его неверным UUID или SNI.
4. `observer_lost` и `control_plane_unavailable`: остановить только собственные pilot процессы, оставить de4/клиентскую пробу доступными; evaluator дает unknown, а не target-down.

Watchdog — независимый от SSH/основного runner процесс с deadline и ограниченной командой восстановления; он вооружен и проверен до воздействия. Обычный try/finally без независимого восстановления недостаточен. Восстанавливаются исходно active units, проверяются все восемь служб, новый VLESS HTTP success и accounting delta. Несработавший rollback — явный failure, дальнейшие fault запрещены. Snapshot rollback не заменяет проверку фактического восстановления.

Дополнительные read-only observers RU/RU2/RU3 допустимы, но необязательны для bounded acceptance. Они выдают только typed results. AS/DC/physical-domain без проверенного источника null, независимость не заявляется. Пользовательская линия NUC остается отдельным инструментом и не объявляется проверенной этим пилотом.

## Сравнение, история и затраты

REQ-CONSUL-07: baseline comparator повторяет фактические method/URL role/timeout/expected-status текущей Cactus URL-пробы, установленные чтением публичного source/config, с секретными значениями только внутри runtime. В отчете указать source revision и параметры без URL-secret. Та же dev target, согласованные интервалы, общий fault timeline. Нельзя выдать произвольный HTTP GET за выполнение существующего мониторинга. Для каждого сценария сравнить наличие/смысл сигнала, detection/recovery и false positives. Различать преимущество новых probes и преимущество Consul хранения/TTL.

REQ-CONSUL-08: export прототип истории/инцидентов — JSON events:

```text
{schema_version:1, event_id, subject:"consul-pilot/<observer>/<target>/<check>",
 measured_at_ms, state:"ok"|"failing"|"unknown",
 transition:"problem"|"recovery"|"observation"|"unknown",
 reason, evidence_ids:[...], origin:"consul-pilot", production_delivery:false}
```

event_id детерминированный SHA256 canonical JSON остальных полей. First fail или pass→fail дает problem; fail→pass recovery; повтор state observation; unknown дает unknown, не закрывает открытый failure. Сохраняется последний известный pass/fail через unknown; последующий pass после fail→unknown дает recovery. Старые measured_at события не меняют newer state, повтор event_id идемпотентен. Это прототип интеграционного контракта, не запись в Cactus DB. Отчет сопоставляет ok/failing с текущим `LineProbe\States`, unknown с inconclusive/probe-loss, описывает отличия серии/дедупликации и необходимый adapter для histories/alerts/triage. Production API/уведомления не вызываются.

REQ-CONSUL-09: измерить peak/sampled RSS, CPU seconds, disk bytes собственных процессов, число checks/API requests, setup/cleanup elapsed. Источник/метод и невозможные измерения явны. Production proposal отдельно: 3–5 server agents, разнесение fault domains, TLS/gossip encryption, ACL/token/cert rotation, upgrades, snapshots/restore rehearsal, self-monitoring, рост probes O(observers×targets×checks), labor tasks и оценка времени диапазоном с допущениями. Один локальный server не доказывает HA/production latency/cost.

## Evidence и закрытие

В output-dir один читаемый `report.md` и `evidence.json` для воспроизводимых тестов. Secrets/runtime dumps не включаются. Каждый scenario записывает mechanism, proof_kind (`real_consul`, `synthetic_fixture`, `real_dev`), repeat, baseline/fault/recovery timestamps, результаты обоих comparators, latency/null+reason, false target-down count, unknown duration, rollback evidence. Report содержит полный mapping REQ-CONSUL-01..09 и INV-CONSUL-01..03 к tests/evidence, revision/hash проверенных файлов, Consul version/hash, ограничения и источники.

Рекомендация выбирается по evidence: integrate (рекомендовать отдельную интеграцию), limit (полезен ограниченно, полная замена не оправдана), reject (не выполнены полезные/эксплуатационные условия). Для каждого выбора указать пользу, стоимость, rollback и следующий production acceptance; рекомендация не предрешена. Непройденный обязательный критерий не закрывается формулировкой limit. Родитель CONSUL-2 остается открыт до доказательства исходных сценариев, интеграционной выполнимости и стоимости. Географическая авария, production HA/rollout и автоматическое переключение не требуются для этого bounded пилота и не объявляются доказанными.

## Слепой RED и независимая приемка

До реализации tests проверяют публичный контракт и семантически фиксируют RED: required heartbeat/types, cross-product missing, cp/ACL partial, TTL expiry != target fail, stale/future boundaries, duplicate/seq replay/conflict, correlated witnesses (включая transitive/unknown metadata), transport pass/application fail, patterns/control evidence, deterministic ordering и причины. Test-writer не получает implementation. Реальный integration test обязан использовать предоставленный Consul, не mock KV. Lab scenario tests проверяют реальные API transitions, ACL deny, cleanup/error paths. Dev contract tests с безопасными adapters проверяют allowlist/preflight failure и watchdog-before-fault; live acceptance остается отдельным доказательством. Полный repository CI и независимая сверка текущей ревизии обязательны перед мержем.

## Инженерные решения и baseline evidence перед freeze

11.10 выполнен ограниченный эксперимент без fault/config/DB changes. Guard: prodrows(de4)=0, dev node42 active с exact domain, существующий единственный is_probe user, Plan возвращает только endpoint42. Credential response захватывался SSH subprocess в RAM; наружу выводились только разрешенные числовые/булевы результаты. Временный client завершен, runtime удален.

- Клиент: `/usr/local/bin/mihomo`, фактическая версия v1.19.25 linux amd64. Повторно используется `CoreProcess` + `parse_vless_uri` из `/data/git/cactus-adm/tools/line-probe/line-probe.py`, source revision `b489495d90f65069b7823cf811cb5da9e8a71597`. Перед CoreProcess runner ставит umask077: temporary directory0700, config0600. Existing dev VLESS TLS профиль идет напрямую в отдельный client, без fallback и изменения работающего mihomo.
- Реальный download: `https://speed.cloudflare.com/__down?bytes=1048576`, SOCKS5 remote DNS через временный VLESS client, timeout25с, response200, получено ровно1048576 bytes. Для acceptance общий бюджет control traffic не более8MiB на цикл baseline/fault/recovery; неограниченное повторение запрещено. Ошибка внешнего HTTP ресурса при отсутствии подтверждения client-stage остается probe_error/unknown; прямой контроль ресурса нужен для интерпретации. Отсутствие HTTP200 само по себе не доказывает отказ Hiddify.
- Panel и counter adapter: канонический HTTPS `GET <api_base_url>/api/v2/admin/user/`, trailing slash, `Hiddify-API-Key` в header, redirects выключены, connectTimeout5с/total10с. Actual response200. В remote PHP выбирается только запись существующего dev is_probe по UUID из dev DB; raw list/UUID/key никогда не выводятся. Метод только читает DB и не вызывает reset usage. Это одновременно structural panel health probe; обязательна проверка JSON-list и наличия ожидаемого пользователя/числового counter.
- Единицы подтверждены installed source `models/user.py`: current_usage — BigInteger bytes, `current_usage_GB = current_usage / (1024**3)`; `to_dict` не округляет property, API schema Float. Adapter возвращает наружу только разность в байтах, признак продвижения и возраст sample; абсолютное значение остается внутренним. Округление delta к ближайшему integer byte допустимо; decrease/reset или нечисловое значение дает unknown, не «учет исправен».
- Доказанный baseline: после1MiB реального VLESS download счетчик вырос на1056954 bytes; новое чтение обнаружило это через33835ms. Counter polling5с. Delta включает protocol overhead/возможную фоновую активность: это подтверждает продвижение учета существующего probe после контролируемого трафика, а не точность биллинга byte-for-byte. Для fault/recovery сравнивать новые окна и сохранять влияние concurrent traffic как ограничение. Celery succeeded по-прежнему только liveness, не замена counter evidence.
- Comparator по зафиксированному root actual config: Mihomo v1.19.25 physical URLTest, GET `https://www.gstatic.com/generate_204`, timeout5с, expected-status `*`, штатный interval30с. В эксперименте оба сравниваемых сигнала идут с согласованным interval5с live/1с lab. Отклонение от штатных30с обязательно в отчете: measured latency не объявляется production latency. Expected-status `*` не подтверждает именно204; richer application probe отдельно проверяет status/body и не выдает это преимущество за свойство Consul.
- Watchdog выбран root: detached Python, pidfd для CONT именно захваченного worker process identity; для HAProxy отдельный watchdog выполняет unit start. Watchdog armed/проверен до воздействия. HAProxy stop максимум30с/watchdog45с; worker STOP180с/watchdog195с. Непроверенная независимость watchdog или невозможность pidfd — preflight failure, без fault fallback на один try/finally.

Технический counter/client/panel adapter доказан baseline. Независимая watchdog-проверка и preflight перед каждым fault остаются обязательной частью исполнения, а не неразрешенной продуктовой развилкой. Freeze/слепой RED выполняет основной агент после сверки этого файла; живые fault здесь не запускались.

## Источники

- CONSUL-2 owner в клиентском vault: `docs/backlog/CONSUL-2-node-monitoring-pilot.md`; CONSUL-1: `docs/done/2026-10-06-consul-node-monitoring.md`.
- Read-only discovery11.10: dev/prod metadata через PHP bootstrap; Plan::build public endpoint summary; de4 systemctl/ss и публичный installed Python source usage.py/user_driver.py/celery.py. Сырые credentials и логи не сохранены.
- [HashiCorp Agent checks](https://developer.hashicorp.com/consul/api-docs/agent/check), [Health API](https://developer.hashicorp.com/consul/api-docs/health), [ACL](https://developer.hashicorp.com/consul/docs/secure/acl), [Reliability](https://developer.hashicorp.com/consul/docs/concept/reliability).


## Уточнения основного агента при freeze

- Закреплен actual Consul2.0.4 linux_amd64, официальный zipSHA2567a28033850a24fd411722593931625d8b548a27646c3ab70c1379ea7fd2af423, binarySHA2569f80affeb492d5e2d8d6c8626107666923e1935ec41c1a3fc6ebcce8846368ec. Бинарник предоставляется извне репозитория; tests не скачивают его молча.
- До livefault dev-only проверяется также по адресам: текущие resolved IPv4/IPv6 de4 не пересекаются ни с одной production Hiddify node, включая доменные алиасы. Неуверенная проверка/неизвестный исход — stop, не оптимистичное разрешение. SSH host key берется из существующего known_hosts, не TOFU. Профиль должен принадлежать существующему dev is_probe user, node42 и hostde4, а не произвольному пользователю.
- Accounting adapter доказан exploratory baseline: canonical HTTPS GET api_base_url+/api/v2/admin/user/, Hiddify-API-Key толькоheader, redirectsdisabled, parseonlydesignatedprobe вRAM. Счетчик current_usage целыеbytes; current_usage_GB=current_usage/1024**3, Floatserializer безокругления. Actual1MiB download черезdevVLESS дал+1056954bytes через33835ms; это baseline, не faultacceptance. Сырые APIответы/URI/UUID не сохраняются. Reset API запрещен.
- Для приложения проверяются два независимых контрольных ресурса. В lab fixture отдельные /healthz (primary) и /payload (secondary); failure толькоprimary при живомsecondary — control_point_only сценарий, не отказ приложения/target. Module combine_application(primary:dict, secondary:dict)->dict принимает две парыstate/reason из указанного enum; если хотябыодинpass — pass/ok, еслиобафail — fail/all_endpoints_failed, иначеunknown/probe_error. all_endpoints_failed добавляется к допустимым fail reasons. Подробности отдельных точек сохраняются только в sanitized scenarioevidence, не расширяют Snapshotv1. Эти правила — модель пилота, не универсальная гарантия SaaSдоступности.
- Baseline сравнения — действительная Mihomo v1.19.25 oneURLhealthprobe: HEADgstatic/generate204, timeout5s, HTTPexpectedstatus*, productioninterval30s. Labmatchedcadence1s/timeout0.5s и livematchedcadence5s/timeout5s явноотделены от productioninterval; вывод о реальнойproductionlatency по acceleratedlab не делается. Локальный fallback сам по себе не объявляет глобальную смерть ноды. Falsepathdown считается только когда singleURLпроба неуспешна, а независимый applicationpath подтвержденноработает; доступность target с другогоobserver сама по себе не делает локальный отказ ложным.
- run_dev включает минимумдва дополнительных реальных read-only observer источника RU/RU2/RU3 через existingSSH: bounded TCP/TLS кde4, typedresults вConsul. Ни агентов/конфигов/файлов/служб на productionисточниках, ни fault там нет. ЕслиAS/DC/physical-domain неизвестны, остаютсяnull и не дают независимогоquorum. Реальныеtransportнаблюдения и LLMVLESS/accounting слоя не смешиваются с synthetic AS/DC evidence.
- Watchdogworker использует kernelpidfds конкретных проверенных workerPID, detachedprocess armed+acknowledged доSIGSTOP; CONTчерезтежеpidfds не может затронутьPIDreuse. HAProxywatchdog ограничен start именноисходноactive devunit. Все проверкиowner/allowlist доотправкисигналов. Harddeadline долженвосстановитьworker≤195s/HAProxy≤45s даже при потере основногоSSH. Послеактивацииwatchdog дополнительныеfault запрещены доуспешногофактическоговосстановления.
- 11.10 пользователь поручил взять задачи в SDD-конвейер и довести до конца; CONSUL-2 выбран для dev-пилота. Production control/routing/alerts остаются вне пилота.


### Runner summary и evidence contract

`run_lab` возвращает dict и CLI печатает один JSON: schema_version=1, mode="lab", outcome="passed"|"failed", acceptance_complete:bool, report:absolute_path, evidence:absolute_path, consul_version:"2.0.4", cleanup:{owned_processes_remaining:int,runtime_secrets_remaining:int}. `run_dev` использует mode="dev", а baseline-only безallow_dev_faults всегда acceptance_complete=false. Любой failedmandatoryscenario запрещает outcome=passed/acceptance_complete=true. Runtime-dir должен быть новым/пустым собственным каталогом, не symlink; неизвестные существующие файлы не удалять. Cleanup удаляет только собственные созданные процессы и runtime секреты, не caller output/report и не произвольные процессы по имени.

Evidence JSON содержит schema_version, mode, consul:{version,binary_sha256,api_reads,api_writes,ttl_expiration_observed,acl_denials:{anonymous_write,evaluator_write}}, scenarios:[{id,repeat,proof_kind,fault_mechanism,injected_at_ms,removed_at_ms,detected_at_ms|null,recovered_at_ms|null,false_path_down:int,unknown_duration_ms:int,rollback:object}], resources:object, cleanup:object, recommendation:"integrate"|"limit"|"reject", limitations:[str], requirement_evidence:object. Промежуточные поля могут быть подробнее; токены/URI/rawresponses/configs запрещены. ACLdenial codes ожидаются403; api counts соответствуют реально выполненным запросам. Минимальные labscenario IDs: healthy, control_point_only, single_path, row_fault, service_down, worker_hang, observer_lost, partition, control_plane_unavailable, recovery; все fault cases по3повтора, healthy baseline>=30s. Превышение15s detection/recovery bound — failure, не увеличениепорога заднимчислом. Devcases изREQ06 по1повтору с ихреальнымиbudget.

Consulbinary SHA должен совпадать с закрепленным до запуска subprocess; подмена stub/fakebinary не может дать принятую integration проверку. Отсутствие бинарника/несовпадениеchecksum — dependencyerrorcode2, не synthetic успешныйrun. Lab самогоConsul реально обновляет/checks register/update/consistentcatalog/TTLexpiration и ACL403; словарь допустим только вpureevaluatorunit tests.

JSON CLI и collector отвергают duplicate object keys. Объем Snapshot ограничен64 observers,64 targets и3 expectedchecktypes, максимум1MiB входногоJSON; больший ввод — structuralerrorcode2. Классификацияpartition должна оставаться ограниченной этимразмером, экспоненциальныйпереборне требуется. Разбиение можно отразить вoptional поле blocks только partitionpattern, дваsortedблока вканоническомпорядке.


### Уточнение REQ08 для слепых unit-тестов

Public `export_events(samples:list[dict])->list[dict]` принимает samples в порядке доставки. Точная sample schema: subject="consul-pilot/<observer>/<target>/<check>", measured_at_ms:int>=0, state="ok"|"failing"|"unknown", reason:один изфиксированныхreasoncodes Snapshot/output, evidence_ids:list[str] (нормализуетсявsortedunique). Unknownfields/invalidtypes/state-reason contradictions отклоняются ValueError с постояннымкодом беззначений.

Output соответствует REQ08. Первыйok — observation, первыйfailing —problem, изменениеok→failing —problem, failing→ok —recovery; повторизвестногоstate —observation. Любойunknown —unknown и не меняетпоследнийопределенныйstate. Поэтому failing→unknown→ok даетrecovery. Older measured_at относительноужеобработанногоsubject игнорируется целиком. Identical normalizedsample повторнонеэкспортируется; одинsubject+timestamp сразнымиstate/reason/evidence — ValueError EVENT_CONFLICT, не произвольныйвыбор. Наinvalidinput функцияничегонепубликует (purefunction). Outputlist сохраняетпорядокприема оставшихсяsamples. EventID=SHA256 UTF8canonicalJSON остальныхoutputполей (sort_keys=True,separators=(',',':'),ensure_ascii=False). Чистаяфункциянеобещаетpersistentconsumerdedup послеrestart: будущийCactusadapterдолженхранитьeventid/laststate. `evidence.json` содержитполе events сэтимexport, дополнительныхфайловсекретоввoutputнет.

Correction baseline по actual pinnedsource: Mihomo v1.19.25 использует **HEAD**, неGET, и не следуетredirects; expectedstatus* допускаетлюбойHTTPstatus. Источник https://github.com/MetaCubeX/mihomo/blob/v1.19.25/adapter/adapter.go (URLTest, http.MethodHead, CheckRedirectErrUseLastResponse, expectedStatusCheck). Это фактическоеуточнениесравнениядоimplementation, анеизменениетестоврадизеленогорезультата.
