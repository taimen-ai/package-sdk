---
name: author-work
description: Описать работу в пакете каталога — типы задач (TaskType с полями, жизненным циклом, согласованиями и исходами, работой после завершения, критериями приёмки, инструкциями исполнителю), роли (Role) и типы артефактов (ArtifactType) — тестами subject taskType раньше описания, проверкой pkg_check и прогоном pkg_test. Используй, когда человек описывает, какую работу делают люди и агенты, кто её согласует и что считается сделанным, или когда процессу или правилу нужен свой тип задачи.
---

# Работа: типы задач, роли, артефакты

Работа — это задачи, которые делают люди и агенты. Её форму задают три вида:

| Вид | Папка | Что задаёт |
|---|---|---|
| `TaskType` | `task-types/` | поля задачи, статусы и переходы, согласования и их исходы, работа после завершения, критерии приёмки, инструкции исполнителю |
| `Role` | `roles/` | роль, на которую назначают шаги, согласования и уведомления |
| `ArtifactType` | `artifact-types/` | вид результата работы: схема `metadata`, допустимые media types, лимит размера |

Схема — `schema/v1/object.schema.json` package-sdk (`$defs.taskTypeSpec`, `roleSpec`,
`artifactTypeSpec`). Заготовку объекта даёт `package-sdk add TaskType <ключ> --package
<пакет>` (так же `Role`, `ArtifactType`).

## Правило плагина

Этот скилл правит файлы пакета и гоняет проверки; на стенд он не пишет. Установка —
только скиллом `simulate-and-plan` после показа плана и явного «да» человека.

## Порядок

1. **Выясни работу словами.** Что за задача и кто её делает (роль, агент); какие поля у
   неё есть и какие обязательны; какие статусы проходит; кто и когда её согласует и что
   происходит при одобрении и отказе; что считается «сделано» (критерии приёмки); что
   надо завести после завершения. Роль — не человек: людей назначает установка.
2. **Поищи готовое.** Тип задачи может уже быть в `requires` (платформенные пакеты,
   свои базовые пакеты). Новый тип — только если работа другая по форме.
3. **Тест раньше описания** — `tests/<сценарий>.test.yaml` с `subject: taskType`: исход
   каждого решения, каждый критерий приёмки, работа после завершения. Образец:

   ```yaml
   subject: taskType
   taskType: request-approval
   name: an approved request is summarized and completed
   given:
     task:
       assignee: alice
       customFields: {request: R-4}
     principals: {reviewer: [alice, bob]}
   mocks:
     skills:
       request.summarize@1:
         - output: {summary: request R-4}
   steps:
     - approve: {decision: approved, by: bob}
     - expect:
         invokeSkill:
           - {skill: request.summarize@1, inputs: {request: R-4}}
         status: {category: terminal_success}
   ```

   Шаги: `approve` (решение по воротам `gate`), `verify` (итог критерия приёмки),
   `complete` (выход завершения по `completionSchema`), `expect` (`status`,
   `invokeSkill`, `ensureWork`, `comments`, `noSideEffects`).
4. **Опиши тип.** Обязательны `displayName` и `lifecycleSchema`:
   - `fieldSchema` — JSON Schema `customFields`;
   - `lifecycleSchema` — `statuses` (`key`, `category`: `backlog | active | blocked |
     terminal_success | terminal_cancelled`), `transitions`, `initialStatus`,
     `claimStatus`, `releaseStatus`, `completionStatus`;
   - `approvalSchema.gates.<ворота>.outcomes` — действия исходов `approved`/`rejected`:
     `invokeSkill` (с `onSuccess`/`onFailure`), `completeTask`, `comment`, `ensureWork`;
     значения из задачи — путями `$.task.customFields.<поле>` (`!` — обязательно);
   - `completionSchema: {onComplete: {when?, actions}}` — работа после завершения
     (`ensureWork` с `customFields`, `relation`, `requestApproval`; `comment`);
   - `acceptance` — критерии приёмки по умолчанию (`key`, `kind`: `human`,
     `deterministic`, `external_state`, `llm_judge`; `description`, `spec`, `when`);
   - `instructions` — Markdown инструкции исполнителю (до 16 КиБ), без секретов;
   - `artifactSchema` — какие артефакты задача принимает и выдаёт;
   - `contextSchema` — какой контекст памяти видит исполнитель.
5. **Роль** — `Role` с `name` и `description`; `key` — slug, на него ссылаются
   `assign: [{role: <slug>}]` процессов и `recipient.kind: role` уведомлений.
6. **Тип артефакта** — `ArtifactType` с `displayName`, `metadataSchema`, `mediaTypes`,
   `maxBytes`.
7. **Проверь и прогони:** `pkg_check` с `path` пакета, затем `pkg_test` (скилл
   `validate-and-fix`). Сценарии типов задач песочница исполняет кодом ядра в
   откатываемой транзакции: ей нужна пустая база PostgreSQL
   (`PACKAGE_SDK_SANDBOX_DATABASE_URL`), без неё они `skipped` — прогон не зелёный.

## Чек-лист

- [ ] У каждого исхода согласования и каждого критерия приёмки есть сценарий.
- [ ] Поля, которые читают исходы и правила, объявлены в `fieldSchema`.
- [ ] У жизненного цикла есть статус с категорией `terminal_success` и переходы в него.
- [ ] Скиллы исходов объявлены в пакете или его `requires` (`kind: Skill`).
- [ ] Роли назначений — объекты `Role` пакета или его `requires`.
- [ ] Изменение поведения опубликованного типа — новая версия, а не правка старой.

## Частые ошибки

- Назначать работу на человека в пакете: пакет знает роли, людей — установка.
- Исход, который читает поле, не объявленное в `fieldSchema`.
- Жизненный цикл без пути из начального статуса в завершённый.
- Секреты и адреса стенда в `instructions`.

## Эталоны

Пути — в репозитории package-sdk.

- Тип задачи с воротами и скиллом исхода (ответ уходит во внешнюю систему только по
  решению человека): `examples/claims/claims/task-types/claim-reply.yaml`, сценарии —
  `examples/claims/claims/tests/claim-reply-approved.test.yaml`,
  `claim-reply-rejected.test.yaml` и `claim-reply-not-sent.test.yaml` рядом.
- Задачи шагов процесса: `examples/claims/claims/task-types/claim-review.yaml`; задача,
  которую заводит правило, — `examples/claims/claims/task-types/claim-followup.yaml`.
- Роли: `examples/claims/claims/roles/claims-officer.yaml` и
  `examples/claims/claims/roles/claims-manager.yaml`.
- Критерий приёмки (в примере его нет):
  `tests/fixtures/pyramid/packages/review-flow/task-types/request-approval.yaml`.
