---
name: author-package
description: Описать процесс в пакете каталога по схеме языка (вид Process, schema/v1/object.schema.json package-sdk) — два уровня (стадии кейса и блоки исполнения), выражения CEL taimen/1, таблицы решений, согласования, таймеры, память (memory, recall, remember, context), owner, identity — и вносить мелкие правки через package-sdk edit, не переписывая файлы целиком. Используй после тестов (write-tests-first), когда надо создать или изменить описание процесса, пакет, тип задачи или календарь.
---

# Описание процесса в пакете

Процесс — объект каталога вида `Process` в `<пакет>/processes/<ключ>.yaml`.
Язык задаёт схема `schema/v1/object.schema.json` package-sdk (`$defs.processSpec`),
выражения — CEL в профиле `taimen/1` (в ядре он называется `cp/1`).
Исполняет процесс ядро. Работай от тестов: описывай ровно то, чего ждут тесты
(`write-tests-first`), и прогоняй их после каждого крупного шага
(`validate-and-fix`).

## Правило плагина

Этот скилл правит файлы в репозитории. Применение на стенде — только скиллом
`simulate-and-plan` после показа плана и явного «да» человека.

## Новый пакет

Заготовку, которая сразу проходит проверку и свой тест, даёт `package-sdk init <каталог>`
(манифест, процесс-пример с тестом, CI, README); объект любого вида —
`package-sdk add <вид> <ключ> --package <каталог>`. Раскладка:

```text
<пакет>/
  package.yaml            # kind: Package, spec.version 0.1.0, requires
  processes/<ключ>.yaml   # kind: Process
  schemas/<имя>.schema.json   # схема данных дела, если большая (data: {$ref: ../schemas/…})
  task-types/<ключ>.yaml  # типы задач шагов human/approve, если их нет в requires
  roles/<slug>.yaml       # роли назначений
  agents/<key>.yaml       # личность процесса (identity.agent), вид service или agent
  tests/<сценарий>.test.yaml
  README.md               # разделы для людей генерирует package-sdk docs <каталог> --write
  .layout/<ключ>.json     # раскладка схемы — не трогать руками
```

Каждый файл — объект в обёртке, первая строка — ссылка на схему для
редактора (путь к схеме установленного package-sdk пишет `package-sdk add`):

```yaml
# yaml-language-server: $schema=<путь к схеме>/object.schema.json
apiVersion: taimen.ai/v1
kind: Process
key: claim
spec:
  version: 1
  displayName: Претензия клиента
```

Новый файл создавай целиком один раз. Все следующие правки делай операциями
`package-sdk edit` (инструмент `pkg_edit`, ниже).

## Порядок

1. Открой черновик спецификации, список тестов и эталоны (раздел «Эталоны»).
2. Опиши **каркас**: `version`, `displayName`, `identity`, `owner`,
   `calendar`, `data` (JSON Schema данных дела), `start` (`on` + `key` +
   `set`), `correlate` для повторов события-источника, первая стадия.
3. Прогони тесты (`pkg_test`). Каждый тест должен упасть по своей причине —
   `write-tests-first`, шаг 6.
4. Наращивай описание **по одному сценарию**: стадия или шаг → проверка →
   тест. Мелкие добавления делай операциями `package-sdk edit`.
5. Когда тесты зелёные, посмотри покрытие (`pkg_test`, раздел coverage).
   Непройденный шаг, переход, строка таблицы или обработчик — это либо
   недостающий тест, либо лишний элемент.
6. Переходи к `simulate-and-plan`.

## Правка через package-sdk edit

Операции меняют только то, что просили: комментарии, порядок ключей и стиль
остальных строк сохраняются. Перед записью документ проверяется схемой. С
`--dry-run` операция печатает diff и ничего не пишет, с `--json` отдаёт ошибку
машиночитаемо: `{ok: false, error: {code, message, path, hint}}`.

Из MCP те же операции — инструмент `pkg_edit`: `operation` — имя операции,
`options` — её опции по имени без дефисов (`{"file": "…", "in": "review", "step":
"{id: x, set: {a: \"'1'\"}}", "after": "approve-payment"}`, флаг — `true`), `dry_run: true` —
только diff. Править можно только файлы внутри корня сессии (`path_outside_root`
иначе); относительные пути и фрагменты `@<файл>` — от него. Ниже — те же операции
командами CLI.

```bash
package-sdk edit add-step  --file packages/p/processes/x.yaml --in review \
  --step "{id: notify-customer, call: {skill: 'notify.send@1', input: {text: \"'Претензия закрыта'\"}}}" --after approve-refund
package-sdk edit add-stage --file … --stage '{id: refund, entry: "milestone.approved", steps: [{id: pay-refund, human: {taskType: claim-refund, assign: [{role: claims-officer}]}}]}' --after review
package-sdk edit add-decision-row --file … --table approval-level --row '{when: {amount: "[1000000..)"}, then: {approvers: 2}}'
package-sdk edit add-rule  --file … --on-event '{on: {observation: claim.withdrawn}, do: [{id: undo, compensate: all}]}'
package-sdk edit add-form-field --file … --step review-claim --name verdict --schema '{type: string, enum: [ok, wrong]}' --required
package-sdk edit rename    --file … --from review-claim --to check-claim      # дописывает карту migrations
package-sdk edit rename    --package packages/p --kind Process --from old --to new  # renames в package.yaml
package-sdk edit set       --file … --path 'spec.stages[review].exit' --value "data.verdict != ''" --string
```

- Не переписывай файл процесса целиком ради одной правки: пропадут комментарии
  и изменится раскладка diff'а, который увидит человек.
- Переименование элемента — только `rename`. Оно дописывает карту
  `migrations` и переносит координаты в `.layout`. Ручная замена id теряет
  открытые экземпляры.
- Если операции для правки нет (например, правка внутри выражения), `set` по
  пути `spec.…` с индексом или id в скобках.

## Язык: шпаргалка

**Два уровня.** Кейс — `stages` (вход `entry` и выход `exit` — сторожа CEL,
`milestones`, `steps`, `discretionary`, `timers`, `repeatable`). Блоки
исполнения — шаги внутри `steps`/`do`. Шаг — ровно один вид плюс общие поля
`id`, `displayName`, `when`, `input.from`, `output.as`, `export.as`,
`governedBy`, `onCompensate`.

| Вид шага | Что делает |
|---|---|
| `human` | задача человеку: `taskType`, `title`, `form` (schema + uischema), `assign`, `due`, `escalations`, `context` |
| `approve` | согласование: `approvers`, `mode` parallel/sequential, `quorum` all/any/`{atLeast: N}`/`{percent: P}`, `earlyDecision`, `separationOfDuties`, `due`, `onDue` |
| `call` | скилл `name@version`, агент или вложенный процесс; `input`, `timeout`, `due`, `context` |
| `decide` | таблица решений из `spec.decisions`: `{table: <id>}` |
| `recall` / `remember` | чтение памяти через ядро / запись факта или сущности от имени процесса |
| `listen` | первое из событий (`any`), `timeout`, `due`, `onTimeout` |
| `wait` | пауза: длительность или `{at: <CEL>}`; срока (`due`) у паузы нет |
| `set` | запись в данные: путь → CEL |
| `fork` | параллельные ветки, `mode: all` или `compete` |
| `try` | `do` + `retry` (limit, delay, backoff) + `catch` по типу ошибки |
| `do` | вложенная последовательность |
| `raise` / `compensate` | ошибка RFC 7807 / откат сделанного (`all` или список id) |
| `suspend` / `resume` / `complete` | приостановка, возобновление, завершение с `outcome` |

Переходы только структурные: порядок блоков, сторожа, вехи. `goto` нет.

**Выражения CEL.** Переменные: `data` (типы — из `spec.data`), `event`
(`type`, `time`, `payload`…), `step` (`result`, `error`), `task` (`id`,
`assigneeId`, `customFields`…), `stage.<id>.completed`/`.active`,
`milestone.<id>`, `instance` (`id`, `key`, `version`, `startedAt`, `clock`).
Функции: `cal.addWorkdays(ts, n, '<календарь>')`, `cal.isWorkday`,
`cal.workdaysBetween`, `duration('P3D')`, `timestamp('…')`, строки
(`lowerAscii`, `split`, `join`), списки, макросы `all`, `exists`, `map`,
`filter`, `has()`, необязательные значения `data.?x.orValue(…)`.

- Значения `set`, `output.as`, `input` — **выражения**, поэтому строковый
  литерал берётся в кавычки CEL внутри YAML: `set: {status: "'paid'"}`,
  `text: "'Претензия закрыта'"`. Число и `true` пишутся как есть в кавычках YAML:
  `"true"`, `"[]"`.
- Срок: длительность `P2D` или `{at: <CEL, дающий timestamp>}`. Срок от
  даты данных: `due: {at: "cal.addWorkdays(timestamp(data.claim.responseDue), -3, 'workdays')"}`.
  Такой таймер ядро пересчитывает само, когда меняется `data.claim.responseDue`.
- Срок (SLA) шага `human`, `approve`, `call`, `recall`, `listen` и процесса целиком
  (`spec.due`, от старта экземпляра) — те же формы или ровно одна единица из
  `duration`, `workdays`, `workhours` с необязательными `calendar` (по умолчанию
  `spec.calendar`) и `warnBefore` (порог предупреждения: длительность,
  `{workdays: N}` или `{workhours: N}`):
  `due: {workdays: 5, warnBefore: {workdays: 1}}`, `due: {workhours: 8, warnBefore: {workhours: 2}}`.
  Рабочие единицы считает календарь; `workhours` — только календарь с
  `workingHours` (`intervals` обычного дня `{from: "09:00", to: "18:00"}`,
  `weekdays` по дню недели ISO, `shortDayReduction` для сокращённых дней).
- Назначение (`assign`, `approvers`, `owner`, `escalations[].to`) —
  цепочка кандидатов: `{role: slug}`, `{principal: ${VAR}}`, `{agent: key}`,
  `{expr: <CEL>}`. Берётся первый разрешимый.

**Таблица решений** (`spec.decisions[]`): `id`, `hitPolicy` first/unique/collect,
`inputs` (`id`, `expr`, `type`), `outputs` (`id`, `type`), `rules` —
`{when: {<вход>: <ячейка>}, then: {<выход>: значение}}`. Ячейка: `"-"` —
любое значение, литерал, список `"a,b"`, диапазон `"[0..1000000)"`. Таблица
читает только данные экземпляра: память попадает в неё через предшествующий
`recall` с `output.as`. Последняя строка `first`-таблицы с `"-"` закрывает
пробелы.

**Память.**
- `memory` процесса — проекция дела в граф: `case` (`kind`, `key` — CEL
  естественного ключа, `title`), `facts` (имя → CEL), `entities` (`kind`,
  `key`, `name`, `rel`, `when`, `many`), `documents.artifacts` (типы
  артефактов).
- `recall` — `anchors` (`{case: true}` или `{kind, key: <CEL>}`), `traverse`
  (`relation`, `direction`, `depth` ≤ 3, `limit`), `query`, `kinds`, `limit`,
  `timeout`, `onTimeout`. Ответ `{nodes, edges, truncated}` пиши в данные
  через `output.as`.
- `remember` — `facts` дела или `entity` (`kind`, `key`, `name`, `text`,
  `links`).
- `context` у `human` и `call` агента — профиль контекста исполнителя
  (`anchors`, `traverse`, `semantic`, `budgetTokens`).
- Виды и связи графа — из онтологий, объявленных в `knowledge` манифеста
  (`KnowledgePack` пакета или его `requires`; платформенная — `process-knowledge`:
  `case`, `lesson`, `regulation`; `applies_to`, `learned_from`…). Не выдумывай
  новые виды в процессе: свой вид — скилл `knowledge-model`.

**Остальное.** `owner` — цепочка назначения владельца процесса (без него
проверка предупреждает). `identity: {agent: <key>}` — от чьего имени действует
процесс; описание агента лежит в пакете или в его `requires`. `governedBy` —
регламенты (`process-from-regulation`). `retrospective` — разбор закрытого
дела. `migrations` — `{from, to, policy: pin|migrate, map}` при новой версии.
`onEvent` — реакции на события всего экземпляра (отмена, приостановка).
Опубликованная версия неизменяема: изменение поведения — это `version: N+1`.

## Чек-лист

- [ ] У каждого элемента стабильный `id` `[a-z][a-z0-9-]*`; id не
      переиспользуются под другой смысл.
- [ ] `start.key` однозначно называет дело; повтор события описан в
      `correlate`.
- [ ] У `human` и `approve` есть `assign`/`approvers`, срок и эскалация, если
      черновик говорит о сроке.
- [ ] Разделение обязанностей из черновика — в `separationOfDuties`.
- [ ] Таблицы решений покрывают все значения входов.
- [ ] Исключения из черновика описаны: `onEvent` с `compensate` и
      `complete`, `suspend`/`resume`, `onCompensate` у шагов, которые надо
      откатывать.
- [ ] `owner` и `identity` есть; `governedBy` — там, где есть регламент.
- [ ] Процесс-цель без конца — без `complete` (скилл `goal-as-process`).
- [ ] Правки после создания файла сделаны операциями `package-sdk edit`.

## Частые ошибки языка

- **`now()` нет.** Время — только `event.time` и `instance.clock`. Условие
  «прошло 3 дня» — это таймер или `wait`, а не сравнение с текущим временем.
- **Типы из схемы данных.** Поле, которого нет в `spec.data`, — ошибка
  проверки `unknown_data_field`. Сначала добавь поле в схему данных. Поле с
  `format: date-time` имеет тип `timestamp`; незаданное поле времени читай
  через `has()` или `.?`.
- **`null` и отсутствующие поля.** Чтение незаданного необязательного поля —
  `expression_error` при исполнении. Используй `has(data.x)` или
  `data.?x.orValue(…)`. Выражение не может заканчиваться необязательным
  значением: нужен `orValue`.
- **Лимит стоимости.** Вычисление дороже 10 000 единиц — ошибка шага
  `expression_cost_exceeded`. Длина выражения — до 4000 символов, глубина — до
  32, вложенность `all`/`exists`/`map`/`filter` — до 3. Большие списки не
  обходи макросами в сторожах: считай нужное шагом `set` один раз.
- **Длительности** — ISO 8601 без лет и месяцев (`P30D`, а не `P1M`).
- **Строковый литерал без кавычек CEL** в `set` и `input`: `text: Готово` —
  это обращение к переменной `Готово`, а не текст.
- **YAML 1.2:** `on`, `off`, `yes`, `no` — строки; булевы значения — только
  `true`/`false`. Номера и коды с ведущими нулями — в кавычках.
- **Порог литералом в сторожe** вместо строки таблицы решений. Порог нельзя
  тогда связать с пунктом регламента, а replay не покажет, чьё решение
  изменилось.
- **`goalId`** в новых описаниях не используй: цель — это
  процесс, скилл `goal-as-process`.

## Эталоны

Пути — в репозитории package-sdk.

- `examples/claims/claims/` — пакет целиком: процесс `processes/claim.yaml` (скилл,
  таблица решений, задачи людей, согласование с разделением обязанностей и
  эскалацией, `memory` и `remember`) с личностью `agents/claims-process.yaml`, типы
  задач, правило, уведомление, онтология, код интеграции со скиллами и наблюдателем,
  образы и сценарии на каждый процесс, правило и тип задачи (`tests/`); установка —
  `examples/claims/packages.yaml`.
- Конструкции, которых в примере нет (таймеры от даты данных, `recall`, `context`,
  `governedBy`, компенсация, приостановка, разбор, миграции):
  `tests/fixtures/process/purchase.process.yaml`, тест —
  `tests/fixtures/process/purchase.test.yaml`, календарь —
  `tests/fixtures/process/ru.calendar.yaml`.
- Схема языка — `schema/v1/object.schema.json`, тестов — `schema/v1/test.schema.json`.
