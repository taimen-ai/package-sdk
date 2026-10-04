---
name: author-agent
description: Описать агента пакета (Agent) — личность (вид agent или service, роли, права, capabilities), какую работу он берёт, чем исполняет (executor с видом, параметрами и образом), какие скиллы исполняет сам, где и сколько работает (placement с метками узла, секретами по имени, ресурсами, репликами) — и проверить pkg_check и pkg_describe. Используй, когда процессу или правилу нужна личность, когда человек хочет исполнителя задач, наблюдателя или хост скиллов.
---

# Агент пакета

Агент — объект `Agent` в `agents/<ключ>.yaml`. Платформа сама заводит его личность
(principal, связку прав) и, если у агента есть `placement`, поднимает исполнителя на
подходящем узле. Менять модель, инструкции или состав агента — правка описания и новая
установка, а не ручная настройка узла.

Схема — `schema/v1/object.schema.json` package-sdk (`$defs.agentSpec`). Заготовка —
`package-sdk add Agent <ключ> --package <пакет>`.

## Правило плагина

Этот скилл правит файлы пакета; на стенд он не пишет. Агент появляется на стенде
только установкой — скиллом `simulate-and-plan` после показа плана и явного «да»
человека. Права агента — часть плана: назови их человеку явно.

## Три формы

| Кто нужен | Форма |
|---|---|
| личность процесса или правила (`identity.agent`) | `identity.kind: service`, `placement: none` — только учётка, без процесса |
| исполнитель задач | `identity.kind: agent`, `work` (какие задачи берёт), `executor` (чем исполняет), `placement` |
| наблюдатель или хост скиллов интеграции | `executor.kind: observer` или `skills`, образ интеграции — скилл `author-integration` |

## Порядок

1. **Выясни словами:** зачем агент; от чьего имени он действует; какие права ему
   нужны (не шире нужного); какие задачи он берёт (workspace, проект, типы задач,
   только назначенные); чем исполняет; где может работать (метки узла, секреты).
2. **Личность** — `identity`: `kind` (`agent` | `service`), `roles` (роли пакета или
   `requires`), `permissions` (права ядра, например `tasks.write`,
   `observations.write`), `capabilities`. Права не шире прав того, кто применяет
   установку.
3. **Работа** — `work`: `workspace`/`project` (переменная установки `${…}` или UUID),
   `includeSubprojects`, `onlyAssigned` (по умолчанию `true`), `taskTypes`.
4. **Исполнитель** — `executor`: `kind` (вид исполнителя; узел знает свой список
   видов), `params` (параметры вида: точка входа наблюдателя, интервал, конфигурация
   со ссылками на переменные), `image` — образ с тегом или дайджестом; узел запускает
   только образы из своего списка разрешённых, иначе `image_not_allowed`.
   У хоста скиллов (`kind: skills`) единственный параметр — `params.env`: несекретные
   настройки скиллов (адрес портала, лимиты), которые хост ставит в окружение каждого
   вызова `local`-скилла. Имена — `[A-Z][A-Z0-9_]*`, до 50 штук, значения — строки до
   2000 символов. Имена вида `*TOKEN`, `*SECRET`, `*PASSWORD`, `*API_KEY`,
   `*CREDENTIALS`, `*PRIVATE_KEY` запрещены — секрет идёт файлом секрета узла
   (`placement.secrets`); имена хоста тоже: семейства `CONTROL_PLANE_*`, `IAM_*`,
   `PYTHON*`, `LD_*`, `DYLD_*`, `GIT_*`, `NODE_*`, `UV_*`, `PIP_*`, `XDG_*` и имена
   `PATH`, `HOME`, `USER`, `SHELL`, `TMPDIR`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`,
   `NO_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`.
   Адрес, который меняется от установки к установке, — переменная установки `${…}`:

   ```yaml
   apiVersion: taimen.ai/v1
   kind: Agent
   key: request-skills
   spec:
     displayName: Request skills
     identity:
       kind: agent
       permissions: [sessions.open, tasks.read, skills.execute]
     executor:
       kind: skills
       params:
         env:
           PORTAL_URL: https://portal.example.com
           PORTAL_PAGE_LIMIT: "50"
     skills:
       protocols: [local]
       local: [intake_demo.skills]
     placement:
       requires: [portal-access]
       secrets: [portal-token]
   ```
5. **Скиллы** — `skills`: протоколы (`local`, `http`, `mcp`), разрешённые точки входа
   `local`, адреса `httpOrigins`/`mcpOrigins`, `invoke` — скиллы, которые агенту
   назначаются при установке.
6. **Размещение** — `placement`: `none` или `{requires: [метки узла], secrets: [имена
   секретов], resources: {cpus, memoryMb}, replicas}`. Секреты — только имена: сам
   материал лежит на узле, в пакет не пишется. `cpus` — целое.
7. **Подключения** — `connections`: ключи подключений tenant'а (до 20, без повторов), к
   материалу доступа которых агент обращается. Подключение заводит администратор по
   типу подключения (`ConnectionType`, скилл `author-integration`); ключ, который не
   `defaultKey` ни одного типа пакета и его `requires`, `check` отмечает
   предупреждением. Непустой список требует у применяющего права `connections.manage`.
8. **Состояние** — `state: running | stopped`.
9. **Проверь:** `pkg_check` с `path` пакета; `pkg_describe` покажет, какие узлы с
   какими метками и секретами нужны установке, — перескажи это человеку.

## Чек-лист

- [ ] Права — минимально нужные; `admin`-прав у агента нет.
- [ ] Секреты названы по имени, материала секретов в пакете нет.
- [ ] Переменные установки в `work` и `params` объявлены в `variables` манифеста.
- [ ] Образ с тегом или дайджестом, не `latest` без тега.
- [ ] Личность процесса или правила — `placement: none`.

## Частые ошибки

- Токен, пароль или адрес с учётными данными в `params` (и в `params.env` хоста
  скиллов) вместо имени секрета.
- Исполнитель без `work.taskTypes`, который заберёт любую задачу workspace.
- Роль, которой нет ни в пакете, ни в `requires`.
- Дробное число `cpus`: ядро принимает только целые.

## Эталоны

Пути — в репозитории package-sdk.

- Личность процесса: `examples/claims/claims/agents/claims-process.yaml`; личность
  правила — `examples/claims/claims/agents/claims-rules.yaml`.
- Наблюдатель с образом, метками и секретом:
  `examples/claims/claims/agents/helpdesk-observer.yaml`; хост скиллов пакета —
  `examples/claims/claims/agents/claims-skills.yaml`.
- Исполнитель задач с рабочей копией (в примере его нет):
  `tests/fixtures/agents/universal-coder.yaml`.
