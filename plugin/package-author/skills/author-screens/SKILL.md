---
name: author-screens
description: Описать экраны пакета — виды View (список экземпляров процесса, карточка экземпляра, очередь задач) и компоненты Component из блоков набора версии 1 с форматами значений, языки package.yaml (locales, defaultLocale) и словари i18n/<locale>.yaml с подстановками ICU — с проверкой pkg_check и планом ядра. Используй, когда человек просит показать работу пакета в консоли — список, доску, карточку, показатели, — или перевести экран на другой язык.
---

# Экраны пакета: виды, компоненты, словари

Экран пакета — **описание**, а не код: вид `View` говорит, что показать (источник,
блоки, форматы значений), консоль рисует его своими блоками. Кода интерфейса в пакете
нет: поле `code` у вида и компонента ядро отвергает (`component_code_not_supported`).
Все строки, которые видит человек, — ключи словарей пакета на объявленных языках.

| Что | Где | Что задаёт |
|---|---|---|
| `View` | `views/` | экран: заголовок, пункт меню, аудитория, источник, параметры, блоки |
| `Component` | `components/` | повторяемый кусок экрана из тех же блоков с параметрами; ядро встраивает его в каждый вид, который его называет |
| словарь | `i18n/<locale>.yaml` | плоское отображение ключ → текст одного языка |
| языки | `package.yaml` | `locales: [en, ru]` и `defaultLocale` из них |

Форма вида и компонента — `schema/v1/view.schema.json` package-sdk (`$defs.viewSpec`,
`$defs.componentSpec`): побайтная копия схемы ядра. Вид и его компоненты ставит и
выводит план ядра (`pkg_plan`), как процессы: пакет с экранами уходит в него целиком,
вместе со словарями.

## Правило плагина

Этот скилл правит файлы пакета и гоняет проверки; на стенд он не пишет. Установка —
только скиллом `simulate-and-plan` после показа плана и явного «да» человека.

## Порядок

1. **Выясни экран словами.** Чьи записи он показывает (экземпляры какого процесса,
   задачи какого типа, записи базы знаний), один экземпляр или много, кто его видит
   (роли организации), где он в меню консоли, что человек открывает по щелчку.
2. **Объяви языки** в `package.yaml` и заведи словарь на каждый:

   ```yaml
   apiVersion: taimen.ai/v1
   kind: Package
   key: payment
   spec:
     version: 1.0.0
     displayName: Payment
     locales: [en, ru]
     defaultLocale: en
   ```

   Ключ строки — латиницей с префиксом пакета (`payment.list.title`); текст — до 2000
   символов, подстановки ICU (`{n}`, `{count, plural, one {# счёт} other {# счетов}}`),
   `'` экранирует скобку. Ядро проверяет только парность скобок, форматирует консоль.
3. **Опиши вид.** Источник — ровно один из `{process, filter?}` (много экземпляров),
   `{process, instance: param.<имя>}` (один экземпляр; параметр `id` можно не
   объявлять), `{tasks: {type}}`, `{knowledge: {kinds}}`. Процесс источника — в пакете
   или в его `requires`. Список экземпляров и карточка:

   ```yaml
   apiVersion: taimen.ai/v1
   kind: View
   key: payment-list
   spec:
     title: payment.list.title
     nav: {group: work, icon: receipt, order: 30}
     audience: {roles: [accounting]}
     source:
       process: payment
       filter: "status == 'running'"
     layout:
       - block: metrics
         items:
           - {title: payment.list.open, value: "count()"}
           - {title: payment.list.total, value: "sum(data.amount)", format: money}
       - block: table
         columns:
           - {field: data.number}
           - {field: data.supplier}
           - {field: data.amount, format: money}
           - {label: payment.list.stage, field: stage, format: status}
         filters: [data.currency, stage]
         sort: [{field: data.amount, dir: desc}]
         open: {view: payment-card, id: id}
   ```

   ```yaml
   apiVersion: taimen.ai/v1
   kind: View
   key: payment-card
   spec:
     title: payment.card.title
     source: {process: payment, instance: param.id}
     layout:
       - block: header
         title: data.number
         status: stage
         actions: steps
       - block: fields
         section: payment.card.payment
         items:
           - {field: data.review}
           - {field: data.paymentReference}
       - block: steps
       - block: timeline
   ```

4. **Подписи.** Колонка — `{label?, field | value, format?, key?}`: `field` — путь
   источника, `value` — выражение CEL, ровно одно. Подпись не написана — это ключ
   `<пакет>.fields.<путь>` (`field: data.amount` → `payment.fields.amount`); у `value`
   — по единственному полю, которое оно читает, иначе подпись пишется сама. Тот же
   ключ — подпись фильтра и сортировки; подпись значения фильтра —
   `<пакет>.fields.<путь>.<значение>`, если она есть в словаре.
5. **Компонент** — описание из тех же блоков с параметрами `{type}` или `{schema}`
   (JSON Schema на месте или `{$ref: ../schemas/<файл>#<указатель>}` файла пакета);
   вид даёт ему значения в `with`. Компонент из компонентов не бывает
   (`nested_component`):

   ```yaml
   apiVersion: taimen.ai/v1
   kind: Component
   key: payment-summary
   spec:
     params:
       payment:
         required: true
         schema: {type: object, properties: {supplier: {type: string}, amount: {type: number}}}
     layout:
       - block: fields
         section: payment.card.summary
         items:
           - {value: param.payment.supplier}
           - {value: param.payment.amount, format: money}
   ```

   В виде: `- {block: component, component: payment-summary, with: {payment: data}}`.
6. **Проверь** — `pkg_check` с `path` пакета (скилл `validate-and-fix`), затем план
   ядра — скилл `simulate-and-plan`.

## Набор блоков версии 1

| Блок | Поля | Где |
|---|---|---|
| `table`, `list` | `columns`, `open?`, `filters?` (пути), `sort? [{field, dir?}]`, `pageSize?` | много записей |
| `board` | `columns: stages`, `card {title, subtitle?, fields?, badge?}`, `open?`, `filters?` | процесс, много экземпляров |
| `header` | `title` (путь), `status?` (путь), `actions?: steps` | один экземпляр |
| `fields` | `section?`, `items` — колонки | один экземпляр |
| `timeline`, `steps` | — | один экземпляр процесса |
| `artifacts` | `types?` — ключи типов артефактов | |
| `related` | `knowledge {kind, key}`, `include? {relations, direction?, limit?}` | |
| `metrics` | `items: [{title, value, format?, key?}]`, `value` — агрегат | |
| `chart` | `chart: bar \| line \| donut`, `groupBy`, `value` (агрегат), `label?`, `format?` | |
| `invoke` | `label`, `skill: <имя>@<версия>`, `input?` | |
| `component` | `component`, `with?` | |

У всех, кроме `header` и `component`, — `title?` (ключ словаря). Агрегаты `count()`,
`count(<условие>)`, `sum/avg(<число>)`, `min/max(<значение>)` пишутся только в
`metrics` и `chart`. Пути процесса — `data.*` по схеме его данных, `instance.{id, key,
version, startedAt, clock}`, `stage`, `status`, `slaState`, `id`. `nav.group` —
закрытый список меню консоли: `work`, `knowledge`, `packages` (нет — `packages`).

Форматы: `text`, `number`, `money`, `percent`, `date`, `datetime`, `due`, `duration`,
`principal`, `status`, `link`. Формат должен подходить типу значения: `money` — число
или строка десятичной записи (`decimal(data.price)`), `date`/`datetime`/`due` — время
или строка, `duration` — длительность или строка, остальные текстовые — строка.

## Находки

`pkg_check` и план ядра отдают находку с файлом и путём (`/spec/layout/1/columns/2/format`;
у словаря — ключ строки):

| Код | Что исправить |
|---|---|
| `missing_message` | ключа нет в словаре объявленного языка — добавь его (в сообщении — какого) |
| `unused_message` | предупреждение: ключ словаря не показывает ни один вид и компонент |
| `undeclared_locale` | словарь языка, которого нет в `locales` |
| `locales_required`, `invalid_locales`, `invalid_default_locale`, `missing_dictionary` | языки `package.yaml` и их словари |
| `invalid_dictionary`, `invalid_message` | файл — не `i18n/<locale>.yaml` с отображением; ключ не по форме, текст не строка, непарные скобки |
| `unknown_block`, `unknown_format` | блока или формата нет в наборе версии 1 |
| `aggregate_outside_metrics` | агрегат вне `metrics` и `chart` |
| `unknown_view` | `open.view` — не вид пакета и не вид пакета из `requires` |
| `unknown_source` | процесса или типа задачи источника нет в пакете и его `requires` |
| `unknown_component`, `nested_component`, `component_code_not_supported` | компоненты |
| `undeclared_path`, `expression_syntax_error`, `expression_type_error`, `format_type_mismatch`, `invalid_aggregate`, `missing_label` | пути и выражения по схеме данных процесса — проверка ядра |

Пути `data.*` и выражения CEL проверяет код ядра — тот же, что при плане. Если
package-sdk стоит с ядром старше экранов, `pkg_check` предупреждает
`screens: data paths, CEL expressions, formats and default labels of views are not checked here`
— их найдёт план ядра.

## Чек-лист

- [ ] В `package.yaml` есть `locales` и `defaultLocale`, на каждый язык — словарь.
- [ ] Каждый ключ — во всех словарях; лишних ключей нет.
- [ ] Источник вида — процесс или тип задачи пакета или его `requires`.
- [ ] `open.view` ведёт на вид пакета или его `requires`.
- [ ] Агрегаты — только в `metrics` и `chart`.
- [ ] В виде нет кода и текстов — только ключи словаря.

## Частые ошибки

- Текст вместо ключа в `title` или `label`: ключ ищется в словаре и не находится.
- Колонка без подписи с `value`, которое читает два поля, — подпись не из чего взять.
- Таблица у вида одного экземпляра или `header` у списка (`block_source_mismatch`).
- Словарь `i18n/ru-RU.yaml` при `locales: [en, ru]` — это другой язык.

## Эталоны

Пути — в репозитории package-sdk.

- Пакет с экранами — список экземпляров процесса оплаты счёта поставщика с
  показателями и фильтрами, карточка экземпляра с компонентом, словари en и ru:
  `tests/fixtures/screens/packages/`. Виды выше — сокращённые его виды; ключ пакета и
  процесса в тексте скилла — `payment`.
- Схема видов и компонентов: `schema/v1/view.schema.json`.
