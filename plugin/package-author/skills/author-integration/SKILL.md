---
name: author-integration
description: Собрать интеграцию с внешней системой пакетом — класс (виды наблюдений, скиллы, онтология) и провайдер (наблюдатель на package_sdk.connector, скиллы на skill-sdk, описание агента-наблюдателя и хоста скиллов с образами), unit-тесты кода и контракты скиллов в пирамиде pkg_test, Dockerfile образов package-sdk image. Используй, когда человек хочет, чтобы платформа видела события внешней системы (CRM, почта, учётная система, портал) или действовала в ней скиллами.
---

# Интеграция: наблюдатель и скиллы в пакете

Интеграция — пакет с кодом. Её части:

- **класс** — что видит и умеет платформа независимо от поставщика: виды наблюдений
  (`<система>.<что>_<событие>`), контракты скиллов (`kind: Skill`), онтология, если
  наблюдения попадают в базу знаний. Класс можно держать отдельным пакетом, чтобы
  провайдеры разных систем ставились взаимозаменяемо;
- **провайдер** — код для конкретной системы: наблюдатель (`package_sdk.connector`) и
  скиллы (`skill-sdk`) в `integration/src/<модуль>/`, их unit-тесты в
  `integration/tests/`, описания агентов-исполнителей (`agents/`) с образами.

Заготовку с наблюдателем и описанием агента даёт `package-sdk init <каталог>
--integration` (с `--image` — ещё Dockerfile).

## Правило плагина

Этот скилл правит файлы пакета и гоняет его код и тесты локально; на стенд он не пишет.
Агенты интеграции появляются на стенде только установкой — скиллом `simulate-and-plan`
после показа плана и явного «да» человека. Скилл с внешней записью
(`external_write`) без согласования человека не описывай: спроси, кто и как одобряет
запись во внешнюю систему.

## Порядок

1. **Выясни словами:** какая система; что из неё надо видеть (события, записи,
   документы) и как часто; что в ней надо делать; как в неё входить (токен, ключ —
   секрет узла по имени); из какой сети она доступна (метка узла).
2. **Класс:** виды наблюдений и их `data`, контракты скиллов. Имена видов — без
   названия поставщика, если класс общий.
3. **Наблюдатель** — функция с декоратором `@observer(kind=…, entrypoint="модуль:функция")`,
   которая за цикл читает систему и публикует наблюдения:

   ```python
   from package_sdk.connector import Observation, ObserveContext, observer, run


   @observer(kind="<агент>", entrypoint="<модуль>.observer:observe")
   def observe(ctx: ObserveContext) -> None:
       token = ctx.secret("<имя-секрета>")  # секрет узла, перечитывается
       cursor = ctx.state.get("cursor")  # состояние между циклами
       for item in fetch(ctx.config, token, cursor):
           ctx.emit(
               Observation(
                   kind="<система>.<вид>",
                   dedup_key=f"<система>:{item.id}",
                   data=item.data,
                   external_ref={"system": "<система>", "id": item.id},
               )
           )
       ctx.state["cursor"] = ...


   if __name__ == "__main__":
       run(observe)
   ```

   Повтор наблюдения с тем же `dedup_key` — дубль, а не новое. Снимки знаний —
   `ctx.snapshot(Snapshot(…))`, документы — `ctx.document(Document(…))`.
   Конфигурация — `executor.params.config` описания агента (адреса — переменными
   установки), секреты — только через `ctx.secret`/`ctx.secret_file`.
4. **Скиллы** — функции `@skill("<имя>", version="1", side_effects=…, risk=…)` из
   `skill_sdk` с моделями входа и выхода. Файлы `skills/<имя>.yaml` пакета генерирует
   `skill-sdk export` из кода: правь код, а YAML перегенерируй — ступень `skills`
   пирамиды сверяет их (`skill-sdk export --check`).
5. **Тесты кода** — `integration/tests/`: наблюдатель — одним циклом на поддельном ядре
   (`package_sdk.connector.testing.run_once(observe, config=…, secrets=…)`), скиллы —
   вызовом функции. Зависимости `integration/pyproject.toml` поставь в окружение сам:
   пирамида их не ставит.
6. **Агенты** — скилл `author-agent`: наблюдатель — `executor.kind: observer`,
   `params.entrypoint`, `config`, `intervalSeconds`; хост скиллов —
   `executor.kind: skills`; `placement` с меткой сети системы и именами секретов.
7. **Образы** — `package-sdk image observer --package <пакет> --entrypoint <модуль:функция>`
   и `package-sdk image skills --package <пакет> --modules <модули>`: Dockerfile от
   базового образа поставки. Тег образа — в `executor.image` описания агента; узел
   запустит его, только если образ в его списке разрешённых.
8. **Пирамида:** `pkg_test` с `path` пакета — проверка, контракты скиллов, pytest кода
   интеграции, сценарии. Всё должно быть `passed`.

## Чек-лист

- [ ] Секреты — только именами (`placement.secrets`, `ctx.secret`), в коде и YAML их нет.
- [ ] У каждого наблюдения `dedup_key`; повтор цикла не множит наблюдения.
- [ ] YAML скиллов сгенерирован из кода и сходится с ним (`skills` — `passed`).
- [ ] Наблюдатель и скиллы покрыты тестами кода (`integration` — `passed`).
- [ ] Адреса системы — переменные установки, объявленные в манифесте.
- [ ] Запись во внешнюю систему — с согласованием человека.

## Частые ошибки

- Токен в `config` или в коде вместо секрета узла.
- Правка `skills/*.yaml` руками: следующий `skill-sdk export` её сотрёт, а `--check`
  упадёт.
- Имя поставщика в видах наблюдений класса — провайдеров потом не заменить.
- Код интеграции, который тянет зависимости, не объявленные в `integration/pyproject.toml`.

## Эталоны

Пути — в репозитории package-sdk.

- Код интеграции целиком: `examples/claims/claims/integration/` — наблюдатель
  `examples/claims/claims/integration/src/claims_helpdesk/observer.py`, скиллы
  `examples/claims/claims/integration/src/claims_helpdesk/skills.py`, их тесты —
  `examples/claims/claims/integration/tests/`.
- Агент наблюдателя — `examples/claims/claims/agents/helpdesk-observer.yaml`, выгрузки
  скиллов — `examples/claims/claims/skills/claims.classify.yaml` и
  `examples/claims/claims/skills/helpdesk.reply.yaml`.
- Образы: `examples/claims/claims/Dockerfile`, `examples/claims/claims/Dockerfile.skills`
  и `examples/claims/claims/.dockerignore` — вывод `package-sdk image` как есть.
