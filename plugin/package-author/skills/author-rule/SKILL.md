---
name: author-rule
description: Описать правило вывода работы (WorkRule) — по наблюдению, событию или расписанию, с условием, разбором скиллом и действием ensure_work, update_work, cancel_work, complete_work или request_decision, от имени личности правила — тестами subject rule раньше описания, проверкой pkg_check и прогоном pkg_test. Используй, когда человек говорит «когда приходит …, заведи задачу …», «если … — закрой работу», «раз в сутки проверяй …».
---

# Правило вывода работы

Правило — объект `WorkRule` в `rules/<ключ>.yaml`: на что реагирует (`trigger`), при
каком условии (`condition`), чем разбирает вход (`interpretation` — скилл) и какую
работу заводит, меняет или закрывает (`action`). Правило — для реакции «событие →
работа». Если у дела есть стадии, сроки и согласования, нужен процесс
(`describe-process`), а не цепочка правил.

Схема — `schema/v1/object.schema.json` package-sdk (`$defs.workRuleSpec`). Заготовка —
`package-sdk add WorkRule <ключ> --package <пакет>`.

## Правило плагина

Этот скилл правит файлы пакета и гоняет проверки; на стенд он не пишет. Установка
правила — только скиллом `simulate-and-plan` после показа плана и явного «да»
человека. Правило, которое заводит работу массово, покажи человеку отдельно: сколько
задач оно заведёт на существующих данных, спроси до плана.

## Порядок

1. **Выясни словами:** источник (наблюдение коннектора `observation` с его `type`,
   событие ядра `event`, расписание `schedule`); условие; нужен ли разбор текста
   скиллом; какая работа и с какими полями; как не завести дубль (ключ
   дедупликации); от чьего имени действует правило.
2. **Тест раньше описания** — `tests/<сценарий>.test.yaml` с `subject: rule`: вход
   `given.observation`, `given.event` или — у правила по расписанию — `given.schedule`
   (слот `{at: "2026-10-01T09:00:00Z"}` в кавычках или `{}` — слот на `clock`); задача,
   которая уже есть до входа, — `given.task` (`type` обязателен; событие без `taskId` —
   о ней). Ответы скиллов в `mocks.skills`, ожидания `result`, `invokeSkill`,
   `ensureWork`, `noSideEffects`. На каждую ветку условия — сценарий, включая «не
   сработало». Образец:

   ```yaml
   subject: rule
   rule: request-reopened
   name: a reopened request is classified and filed for approval
   given:
     observation:
       kind: request.reopened
       content: the answer did not help
       data: {request: R-2, channel: web}
   mocks:
     skills:
       request.classify@1:
         - output: {category: complaint, confidence: 0.9}
   steps:
     - expect:
         result: matched
         invokeSkill:
           - skill: request.classify@1
             inputs: {text: the answer did not help}
         ensureWork:
           - type: request-approval
             customFields: {request: R-2, category: complaint}
   ```

3. **Опиши правило:**
   - `trigger` — `{kind: observation, type: …}`, `{kind: event, …}` или
     `{kind: schedule, …}`;
   - `condition` — грамматика правил ядра: `and`, `or`, `not`, `exists`, `eq`, `ne`,
     `var` над `payload`;
   - `interpretation` — `{skill: name@version, inputs: {…}}`; выход — `skill.output.*`
     в шаблонах действия;
   - `action.kind` — `ensure_work` (заводит работу один раз на `dedupKeyTemplate`),
     `update_work`, `cancel_work`, `complete_work`, `request_decision`; `taskType` и
     `fields` (`title`, `customFields`, `relations`) — шаблонами `{{payload.…}}`;
   - `identity: {agent: <ключ>}` — личность правила: описание агента вида `service`
     (`placement: none`) в пакете или `requires`; без неё правило действует
     полномочиями того, кто его применил;
   - `status: disabled` — если правило ставится выключенным до решения человека.
4. **Проверь и прогони:** `pkg_check`, затем `pkg_test` (скилл `validate-and-fix`).
   Сценарии правил песочница исполняет кодом ядра в откатываемой транзакции: нужна
   пустая база PostgreSQL (`PACKAGE_SDK_SANDBOX_DATABASE_URL`); без неё сценарии
   `skipped`, и прогон не зелёный.

## Чек-лист

- [ ] У каждой ветки условия есть сценарий, в том числе «не сработало».
- [ ] `dedupKeyTemplate` однозначно называет работу: повтор входа не заводит дубль.
- [ ] Тип задачи действия, скилл разбора и личность правила есть в пакете или `requires`.
- [ ] Поля `customFields` действия объявлены в `fieldSchema` типа задачи.
- [ ] Порог или список значений, который меняется, — переменная установки
      (`variables` манифеста), а не литерал в условии.

## Частые ошибки

- Правило без ключа дедупликации: каждый повтор наблюдения — новая задача.
- Процесс, собранный из десятка правил: сроки и согласования — дело процесса.
- Условие по полю, которого в наблюдении нет: `exists` раньше сравнения.
- Действие от имени того, кто применил пакет, там, где нужна отдельная личность.

## Эталоны

Пути — в репозитории package-sdk.

- Правило с условием, разбором скиллом и `ensure_work`:
  `examples/claims/claims/rules/claim-reopened.yaml`, его личность —
  `examples/claims/claims/agents/claims-rules.yaml`; сценарии —
  `examples/claims/claims/tests/claim-reopened.test.yaml` и рядом
  `claim-reopened-internal.test.yaml`, `claim-reopened-unclassified.test.yaml`,
  `claim-reopened-without-ticket.test.yaml`.
