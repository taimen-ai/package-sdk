---
name: author-notification
description: Описать правило уведомления (NotificationRule) — на какое событие ядра и при каком условии, кому (назначенный, роль, владелец или исполнитель задачи, principal из переменной установки), с каким заголовком, текстом, ссылками и кнопками решения, с дедупликацией и закрытием по событию — и проверить его pkg_check. Используй, когда человек говорит «сообщай …, когда …», «напоминай ответственному о сроке», «пусть согласующий решает из уведомления».
---

# Правило уведомления

Правило уведомления — объект `NotificationRule` в `notification-rules/<ключ>.yaml`. Ядро
только публикует события; кому и что доставить, решает сервис уведомлений по этим
правилам. Без применённого правила уведомлений нет.

Схема — `schema/v1/object.schema.json` package-sdk (`$defs.notificationRuleSpec`).
Заготовка — `package-sdk add NotificationRule <ключ> --package <пакет>`.

## Правило плагина

Этот скилл правит файлы пакета; на стенд он не пишет. Правила уведомлений ставятся
установкой (секция `notification-rules` плана, её проверяет сервис уведомлений) —
только скиллом `simulate-and-plan` после показа плана и явного «да» человека.

## Порядок

1. **Выясни словами:** о чём уведомлять (событие ядра: `approval.requested`,
   `task.assigned`, `process.escalated`, …; или префикс `task.*`); при каком условии;
   кому; что написать; нужны ли кнопки решения; когда уведомление устаревает.
2. **Событие** — `on.type` и `on.when` (грамматика правил ядра над `payload`, `event`,
   `task`: `and`, `or`, `eq`, `var`…). Каталог событий и поля `payload` — у ядра
   стенда; процесс публикует `process.*` (например `process.escalated` с
   `payload.definitionKey`, `payload.action`, `payload.level`).
3. **Кому** — `recipient.kind`:
   - `assigned` — тот, на кого назначено (путь к principal в событии — `ref`);
   - `role` — роль (`ref` — путь к роли в событии, `workspace` — путь к workspace);
   - `taskOwner`, `taskAssignee` — владелец или исполнитель задачи события;
   - `principal` — конкретный principal: `ref` — переменная установки `${…}`, не UUID
     в пакете;
   - `fallback` — кому, если адресат не нашёлся (`taskOwner`, `taskAssignee`, `none`).
4. **Что** — `notification`: `type` (по нему работают настройки получателя), `title`
   (до 300 символов) и `body` — шаблоны `{{payload.…}}`, `{{task.…}}`; `links` (до
   пяти), `actions: [approvalDecide]` — кнопки «Одобрить»/«Отклонить» решения из
   `payload.approvalId`.
5. **Без дублей** — `dedupKeyTemplate`: одно уведомление на предмет и уровень.
   `close.on` — события, после которых кнопки уведомления с тем же ключом закрываются
   (`approval.approved`, `approval.rejected`), `close.outcome` — что показать вместо них.
6. **Переменные** — адрес сервиса уведомлений (`NOTIFICATION_SERVICE_URL`) и principal
   адресата — переменные установки; объяви свои в `variables` манифеста.
7. **Проверь:** `pkg_check` с `path` пакета. Форму правила окончательно проверяет
   сервис уведомлений при плане (`:validate`): его отказ — находка плана.

Образец — запрос решения назначенному, с кнопками и закрытием после решения:

```yaml
apiVersion: taimen.ai/v1
kind: NotificationRule
key: approval-requested
spec:
  description: Назначенному — запрос решения
  "on":
    type: approval.requested
  recipient: {kind: assigned}
  notification:
    type: approval.requested
    title: "Нужно решение: {{task.title}}"
    actions: [approvalDecide]
  dedupKeyTemplate: "approval:{{event.entityId}}"
  close:
    "on": [approval.approved, approval.rejected]
```

## Чек-лист

- [ ] Событие и поля условия есть в каталоге событий ядра.
- [ ] Адресат разрешается из события или из переменной установки.
- [ ] `dedupKeyTemplate` включает всё, что отличает одно уведомление от другого.
- [ ] У уведомления с кнопками решения есть `close.on`.
- [ ] В тексте нет персональных данных сверх нужного адресату.

## Частые ошибки

- UUID человека прямо в пакете вместо переменной установки.
- Роль из шага процесса как адресат: событие несёт её текстом `role:<slug>`, а
  адресату `role` нужен id роли по пути в событии.
- Правило без `dedupKeyTemplate`: каждое повторное событие — новое уведомление.
- Кнопки решения без `close.on`: после решения кнопки остаются.

## Эталоны

Пути — в репозитории package-sdk.

- Уведомление о запросе решения, кнопки которого закрываются после решения:
  `examples/claims/claims/notification-rules/claim-reply-approval.yaml`.
