---
id: LLM-LOCAL-FALLBACK
status: done
created: 2026-10-10
---

# LLM: основной EE, резерв DE, затем PL

Пользователь уточнил: EE упала, DE — ее резерв. Текущие локальные правила LLM закрепляют OpenAI/Codex за физической DE (комментарий от10.10 EE exit timeout). Добавляем страховку, сохраняя исходную основную EE и первый резерв DE: EE→DE→PL. Пока EE отсутствует в подписке, группа содержит DE,PL и выбирает DE. После возвращения EE в подписку и успешной проверки она становится первой; существующие TCP-сеансы не гарантированно сохраняются.

Общий опубликованный ee-failover сейчас имеет EE→PL→DE. Не менять предпочтения других машин и маршруты админки ради локальной настройки LLM. Добавить необязательный файл /etc/mihomo/cascade-fallbacks.local.json (под MIHOMO_ETC в тестах), strictJSON object {"ee":["de","pl"]}. Он меняет только перечисленные цепочки поверх корректных центральных cascade-fallbacks; остальные центральные цепочки и cascade-probes сохраняются. Локальная карта не меняет правила автоматически: оператор явно направляет существующие локальные OpenAI/Codex rules в ee-failover вместо физическойde. Физические канарейки сохраняютphysicalalias.

Если файла нет, результат генератора побайтово прежний. Если файл существует, он должен быть непустым объектом с непустыми списками допустимых физических alias, без self/duplicates/derived collisions/unknown/ambiguous aliases. DuplicateJSONkeys, empty/malformed/unreadable input — failclosed code3 с сохранением прежнего config. Не скрывать локальной картой невалидный центральный mapping: сначала проверить центральный, затем локальный и результат объединения. DIRECT/REJECT не могут стать членами fallback; рекурсии нет. Файл читается каждый refresh, изменение только порядка должно менять config/reload по существующему механизму. При отсутствииprimary и живыхreserves применяется новыйконфиг; при отсутствующихвсехmembers прежнийconfigилиREJECT, как существующийконтракт. Прочие node-floor/rules-required/orphan guards сохраняются.

Развертывание толькоLLM: новыйbuilder, root-only0600 JSON, узкая замена двадцати существующих локальных OpenAI rules de→ee-failover с сохранением targets/order/comments и всех остальныхлокальныхправил. Снимокlocal-rules SHA256d66aa3567ffe77c05fd7dc9f0d4c696b96dc5c414c86379357f1242c36c1a2df — предусловие, повторносверитьпередзаписью. Нельзя затирать hostlocalrules общим etc/mihomo/local-rules.yaml. Бэкап rootprivate700 всехизменяемыхисточников/старогоконфига, flockexistingrefreshlock, атомарныезамены, validation/refresh, rollbackприошибке. Ни SSH/sudo/firewall/systemd definitions, ни ноды EE/DE/PL не менять. Резервные credentials не печатать/передавать в review.

## Приемка LLM-LOCAL-FALLBACK

- L01: локальный ee:[de,pl] перекрывает центральный ee:[pl,de]; локальный policy ee-failover и физическая ee-canary не перепутаны, другиецепочки сохранены. При всехmembers порядок EE,DE,PL; при отсутствующейEE —DE,PL, безDIRECT.
- L02: безlocalfile output byteexact; изменение толькоlocalorder меняет output; удалениеlocalfile возвращаетcentralorder. Invalidcentral не обходится validlocal. Invalidlocal types/empty/duplicatekeys/self/duplicates/unknown/unreadable не трогают previousconfig.
- L03: full existing runtime suites GREEN, independent blind RED beforecode, Haiku review. Unknown ordinary orphan/node-floor/rulesrequired guards остаются.
- L04: actualLLM API показывает fallbackEEсmembersDE,PL и выбраннойDE, ранниеOpenAI/ChatGPT rules→ee-failover; обычныйHTTP/SOCKSpath достигаетOpenAI401. Изолированная instance с этой actualгруппой доказывает DE→PL→DE и all-downбезDIRECT, безfaultproduction. ОтсутствующаяEE диагностическиREJECT; liveEEfailbackвДЦ сейчаснепроверяем, приоритетEEпроверяетсяunit/CLIнаsyntheticподписке.

## Источники и границы

Repo mihomo-cascade; currentbase dcf562f8. HostJSON — локальная операторская настройка, не новый канал артефактов для парка. install.sh ее не удаляет; общуюlocal-rules после hostreinstall не считать резервнойкопией LLMoverrides, настройкиLLM сохранятьвprivatebackup/документированномdeployment. Workowner vault docs/done/2026-10-10-llm-local-openai-fallback.md. Rollout других5машин не нужен дляoptionalfeature, пока унихнетlocalfile.

## Приемка и установка завершены

Независимый blind1269149:8 методов/22 содержательных RED до реализации,0 fixture errors. Реализация95d14f9 (actual gpt-6-astra/medium): builder+README. Root независимо выполнил все27 методов(local8/cascade6/rules-required13) и3 shell suites — GREEN. Unknown ordinary orphan, node-floor, required rules сохранены. Функциональные файлы после этого не менялись.

Haiku5.5 actual OpenRouter/Claude Platform on AWS: GREEN, response gen-1791665747-kD8z4W8SvDce89437QcK; проверены runtime и bounded rollout helper af0883c6579eb15b3c9de70a6be0043a7fecc8a0832eff9bd59032aa9ea23ad1. Первичные ложные замечания о probe/reserve disjointness и Python finally закрыты фактической сверкой без изменения кода.

LLM installation: candidate SHA89efa46b9e6529c52200950786e43ec22d4fef2ff9cfaa74509d05d914175418; strictJSONee:[de,pl],20 локальных правил направлены в ee-failover. Dry-run/apply exit0, privatebackup /root/llm-openai-fallback-20261010-235701-wpwyttsn. Обычный production mixed7890 достигает OpenAI API HTTP401; API подтверждает ранниеOpenAI/ChatGPT→ee-failover, физическиеDE,PL, selectedDE. EE физическиREJECT до восстановления ДЦ. Global artifact/другие5машин не изменялись.

Живая изолированная копия actualLLM runtime group (immutableconfig, loopbackrelays,безprod fault): DE→PL30.1с, возвратDE29.1с, all-down29.1с блокируетpayloadбезDIRECT. CFcountryDE/PL/DE, OpenAI401 во всех доступных фазах; reload0/forced-delay0. Лог /tmp/llm-local-live-proof.log. LiveEEfailback не заявляется: EE недоступна в ДЦ, полный приоритетEE/DE/PL покрыт CLI synthetic subscription tests. Путь LLM — обычный localproxy, не temporaryReality из другой задачи.
