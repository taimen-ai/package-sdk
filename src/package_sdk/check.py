"""Проверка пакетов без стенда: схема формата, замкнутость ссылок, доменные валидаторы ядра
(extra sandbox), статика процессов и тестов пакета."""

from __future__ import annotations

from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - окружение без PyYAML
    yaml = None

from package_sdk import schema as schema_module
from package_sdk.manifest import check_manifest, package_env
from package_sdk.model import (
    AGENT_REF,
    API_VERSION,
    CATALOG_KINDS,
    NOTIFY_CONDITION_ROOTS,
    RETIRABLE,
    SYSTEM_TASK_TYPE,
    Installation,
    Obj,
    PackageError,
    _rel,
    substitute,
)

CORE_MISSING = (
    "доменные валидаторы ядра не импортируются — проверена только схема формата; "
    "установите package-sdk[sandbox] (код control-plane закреплённой версии)"
)


def _format_schema_error(error: Any) -> str:
    where = "/".join(str(p) for p in error.absolute_path) or "(корень)"
    return f"{where}: {error.message}"


def _schema_validator() -> Any:
    return schema_module.validator(schema_module.OBJECT)


def _domain() -> Any:
    """Доменные валидаторы control-plane (extra ``sandbox``); None — если не импортируются."""
    try:
        from control_plane.domain import (
            approval_outcomes,
            project,
            skill_contract,
            task_execution,
            work_item,
            work_rules,
        )
        from control_plane.domain.errors import DomainError
    except ImportError:
        return None

    class Domain:
        pass

    domain = Domain()
    domain.approval_outcomes = approval_outcomes
    domain.project = project
    domain.skill_contract = skill_contract
    domain.task_execution = task_execution
    domain.work_item = work_item
    domain.work_rules = work_rules
    domain.DomainError = DomainError
    try:  # профиль контекста — control-plane с CP-ADR-0064
        from control_plane.domain import context_schema
    except ImportError:
        context_schema = None
    domain.context_schema = context_schema
    try:  # CP-ADR-0066: ядро старше инструкций исполнителю этого модуля не знает
        from control_plane.domain import agent_instructions
    except ImportError:
        agent_instructions = None
    domain.agent_instructions = agent_instructions
    try:  # CP-ADR-0061, амендмент 2026-09-25: работа после завершения задачи
        from control_plane.domain import completion_work
    except ImportError:
        completion_work = None
    domain.completion_work = completion_work
    try:  # CP-ADR-0072: типы артефактов и входы/выходы типа задачи
        from control_plane.domain import artifact_schema, artifact_type
    except ImportError:
        artifact_schema = artifact_type = None
    domain.artifact_schema = artifact_schema
    domain.artifact_type = artifact_type
    try:  # CP-ADR-0073: общие разделы описания агента проверяет модель API ядра
        from control_plane.api.v1.schemas import AgentSpec as agent_spec
    except ImportError:
        agent_spec = None
    domain.agent_spec = agent_spec
    return domain


def _literal(value: Any) -> bool:
    return isinstance(value, str) and "$" not in value


def _repository_url_identity(url: str) -> str:
    """Адрес репозитория для сравнения: без учёта регистра, без завершающего /, без .git —
    как сверяет адреса интеграция selfdev суперпроекта (oss_sync._same_repository), но
    регистр снимается первым, чтобы .GIT тоже считался суффиксом."""
    return url.lower().rstrip("/").removesuffix(".git")


def _working_copy_catalog_errors(spec: dict[str, Any]) -> list[str]:
    """Связи каталога workingCopy, которых не выражает JSON Schema (TAI-ADR-0063 п.3).

    Форму проверила схема; здесь — ссылка superproject на ключ каталога и однозначность:
    ключ или псевдоним ведёт ровно к одной записи (сравнение без учёта регистра, casefold —
    так ключ задачи разрешают демон исполнителя и tasks.check@1), адрес после нормализации —
    ровно к одному ключу, каталог в рабочей копии не делят две записи.
    """
    working_copy = spec.get("workingCopy")
    if not isinstance(working_copy, dict) or not isinstance(working_copy.get("repositories"), dict):
        return []
    repositories: dict[str, Any] = working_copy["repositories"]
    errors: list[str] = []
    superproject = working_copy.get("superproject")
    if superproject is not None and superproject not in repositories:
        errors.append(
            f"workingCopy.superproject {superproject!r} — такого ключа нет в workingCopy.repositories"
        )
    # casefold-имя → (запись, псевдоним или None для ключа)
    names: dict[str, tuple[str, str | None]] = {
        str(key).casefold(): (key, None) for key in repositories
    }
    urls: dict[str, str] = {}
    directories: dict[str, str] = {}
    for key, entry in repositories.items():
        entry = entry if isinstance(entry, dict) else {}
        where = f"workingCopy.repositories.{key}"
        aliases = entry.get("aliases")
        for alias in aliases if isinstance(aliases, list) else []:
            if not isinstance(alias, str):
                continue
            taken = names.get(alias.casefold())
            if taken is None:
                names[alias.casefold()] = (key, alias)
                continue
            owner, other = taken
            if owner == key and other is None:
                errors.append(
                    f"{where}.aliases: {alias!r} совпадает с ключом записи без учёта регистра"
                )
            elif owner == key:
                errors.append(
                    f"{where}.aliases: {alias!r} повторяет псевдоним {other!r} этой же записи "
                    "без учёта регистра"
                )
            elif other is None:
                errors.append(
                    f"{where}.aliases: {alias!r} совпадает с ключом записи {owner!r} — "
                    "ключ задачи разрешался бы неоднозначно"
                )
            else:
                errors.append(
                    f"{where}.aliases: {alias!r} совпадает с псевдонимом {other!r} записи {owner!r} — "
                    "ключ задачи разрешался бы неоднозначно"
                )
        url = entry.get("url")
        if isinstance(url, str):
            identity = _repository_url_identity(url)
            if identity in urls:
                errors.append(
                    f"{where}.url совпадает с адресом записи {urls[identity]!r} (без завершающего /, "
                    ".git и регистра) — ключ по адресу определялся бы неоднозначно"
                )
            else:
                urls[identity] = key
        directory = entry.get("directory")
        if isinstance(directory, str):
            folded = directory.casefold()
            if folded in directories:
                errors.append(
                    f"{where}.directory {directory!r} уже занят записью {directories[folded]!r}"
                )
            else:
                directories[folded] = key
            for other_key in repositories:
                if other_key != key and str(other_key).casefold() == folded:
                    errors.append(
                        f"{where}.directory {directory!r} совпадает с ключом записи {other_key!r} — "
                        "в плоской раскладке рабочей копии каталог соседа был бы неоднозначен"
                    )
    return errors


def _flatten(items: Any) -> list[tuple[str, dict[str, Any]]]:
    """Действия списка вместе с реакциями invokeSkill (onSuccess/onFailure)."""
    actions: list[tuple[str, dict[str, Any]]] = []
    for action in items or []:
        if isinstance(action, dict) and len(action) == 1:
            name, inputs = next(iter(action.items()))
            inputs = inputs if isinstance(inputs, dict) else {}
            actions.append((name, inputs))
            for reaction in ("onSuccess", "onFailure"):
                actions += _flatten(inputs.get(reaction))
    return actions


def _outcome_actions(approval_schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    actions = []
    for gate in (approval_schema.get("gates") or {}).values():
        for items in ((gate or {}).get("outcomes") or {}).values():
            actions += _flatten(items)
    return actions


def _completion_actions(completion_schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return _flatten(((completion_schema or {}).get("onComplete") or {}).get("actions"))


def _field_skeleton(schema: Any) -> Any:
    """Ключи документа, который описывает JSON Schema, — без значений.

    Имя поля ядро проверяет только в записанном документе (customFields задачи), а не в
    fieldSchema типа: тип с полем, чьё имя похоже на секрет, публикуется, а задача с ним —
    нет (secret_material_rejected, TASK-001190). Скелет даёт ту же проверку заранее.
    """
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return {str(name): _field_skeleton(sub) for name, sub in properties.items()}
    items = schema.get("items")
    if isinstance(items, dict):
        return [_field_skeleton(items)]
    return None


def _domain_check(obj: Obj, spec: dict[str, Any], domain: Any) -> None:
    """Те же проверки, что делает ядро при создании (раньше, чем их сделает стенд)."""
    work_item, project = domain.work_item, domain.project
    if obj.kind == "TaskType":
        project.validate_json_schema_document(
            spec.get("fieldSchema") or {}, field_name="fieldSchema"
        )
        # Поле, которое ядро не даст записать в задачу (имя похоже на секрет): правило имён —
        # то же reject_secret_material ядра, которым validate_task_custom_fields проверяет
        # customFields, а не копия.
        project.reject_secret_material(
            _field_skeleton(spec.get("fieldSchema")), label="customFields"
        )
        lifecycle = work_item.parse_work_item_lifecycle(
            spec.get("lifecycleSchema") or work_item.SYSTEM_TASK_LIFECYCLE
        )
        domain.approval_outcomes.parse_approval_schema(
            spec.get("approvalSchema") or {}, statuses=frozenset(lifecycle.lifecycle.categories)
        )
        domain.task_execution.normalize_execution(spec.get("execution"))
        if spec.get("contextSchema") is not None and domain.context_schema is not None:
            domain.context_schema.parse_context_schema(spec["contextSchema"])
        if spec.get("completionSchema") is not None and domain.completion_work is not None:
            domain.completion_work.parse_completion_schema(spec["completionSchema"])
        if spec.get("instructions") is not None and domain.agent_instructions is not None:
            domain.agent_instructions.validate_instructions(
                spec["instructions"], field="instructions"
            )
        if spec.get("artifactSchema") is not None and domain.artifact_schema is not None:
            domain.artifact_schema.parse_artifact_schema(spec["artifactSchema"])
    elif obj.kind == "Agent":
        if domain.agent_spec is not None:
            try:
                domain.agent_spec.model_validate(spec)
            except Exception as error:  # pydantic.ValidationError: сообщение ядра как есть
                raise PackageError(f"ядро не принимает описание агента: {error}") from error
    elif obj.kind == "ArtifactType":
        if domain.artifact_type is not None:
            # потолок установки знает только стенд — здесь проверяется всё, кроме него
            domain.artifact_type.validate_artifact_type_definition(
                metadata_schema=spec.get("metadataSchema") or {},
                media_types=spec.get("mediaTypes") or ["*/*"],
                max_bytes=spec.get("maxBytes"),
                global_max_bytes=max(int(spec.get("maxBytes") or 1), 1),
            )
    elif obj.kind == "ProjectTemplate":
        project.validate_json_schema_document(
            spec.get("fieldSchema") or {}, field_name="fieldSchema"
        )
        if spec.get("lifecycleSchema") is not None:
            project.parse_lifecycle(spec["lifecycleSchema"])
        project.validate_config_document(
            spec.get("defaultConfig") or {}, field_name="defaultConfig"
        )
        project.validate_governance(spec.get("governanceSchema") or {})
        project.validate_config_document(
            {"memory": spec.get("memoryDefaults") or {}}, field_name="memoryDefaults"
        )
    elif obj.kind == "WorkspaceType":
        project.validate_json_schema_document(
            spec.get("fieldSchema") or {}, field_name="fieldSchema"
        )
    elif obj.kind == "Skill":
        contract = domain.skill_contract
        if spec.get("contract") is not None:
            normalized = contract.normalize_contract(spec["contract"])
            side_effects, _risk = contract.validate_policy_columns(
                spec.get("sideEffects"), spec.get("riskLevel")
            )
            contract.require_safe_retries(normalized, side_effects)
        elif spec.get("sideEffects") is not None or spec.get("riskLevel") is not None:
            contract.validate_policy_columns(spec.get("sideEffects"), spec.get("riskLevel"))
        for name in ("inputSchema", "outputSchema"):
            if spec.get(name) is not None:
                project.validate_json_schema_document(spec[name], field_name=name)
    elif obj.kind == "WorkRule":
        domain.work_rules.normalize_rule_key(obj.key)
        normalize_rule(spec, domain)
    elif obj.kind == "NotificationRule":
        # Грамматика условия общая с правилами ядра (ADR-0005 §6 п.4); типы событий и пути
        # шаблонов знает только сервис — их проверит :validate перед записью.
        domain.work_rules.normalize_rule_key(obj.key)
        when = (spec.get("on") or {}).get("when")
        if when is not None:
            domain.work_rules.validate_expression(
                when, roots=NOTIFY_CONDITION_ROOTS, where="on.when"
            )


def _without_fields_workspace(action: Any) -> tuple[Any, Any]:
    """action без fields.workspaceId и сам шаблон (None — поля нет)."""
    if (
        not isinstance(action, dict)
        or not isinstance(action.get("fields"), dict)
        or "workspaceId" not in action["fields"]
    ):
        return action, None
    fields = dict(action["fields"])
    template = fields.pop("workspaceId")
    return {**action, "fields": fields}, template


def normalize_rule(spec: dict[str, Any], domain: Any) -> dict[str, Any]:
    """Документы правила в том виде, в каком их хранит ядро (normalize_rule_spec).

    ``fields.workspaceId`` — workspace заводимой работы (амендмент CP-ADR-0063,
    process-packages P012). Ядро до P012 поля не знает (``unknown keys``): тогда оно
    проверяется здесь — строка-шаблон с корнями действия — а остальное действие — ядром."""
    work_rules = domain.work_rules
    action = spec.get("action")
    try:
        normalized = work_rules.normalize_rule_spec(
            trigger=spec.get("trigger"),
            condition=spec.get("condition", True),
            interpretation=spec.get("interpretation"),
            action=action,
        )
    except domain.DomainError as error:
        bare, template = _without_fields_workspace(action)
        if template is None or "workspaceId" not in ((error.details or {}).get("unknown") or []):
            raise
        normalized = work_rules.normalize_rule_spec(
            trigger=spec.get("trigger"),
            condition=spec.get("condition", True),
            interpretation=spec.get("interpretation"),
            action=bare,
        )
        if not isinstance(template, str) or not template.strip():
            raise PackageError(
                "action.fields.workspaceId: ожидается шаблон — непустая строка"
            ) from error
        roots = work_rules.action_roots(
            interpreted=spec.get("interpretation") is not None,
            for_each=bare.get("forEach") is not None,
        )
        work_rules.template_paths(
            template, roots=roots, where="action.fields.workspaceId", code="invalid_rule_action"
        )
        normalized.action.setdefault("fields", {})["workspaceId"] = template
    return {
        "description": domain.work_rules.normalize_description(spec.get("description", "")),
        "trigger": normalized.trigger,
        "condition": normalized.condition,
        "interpretation": normalized.interpretation,
        "action": normalized.action,
        # личность ядро хранит как есть: {agent: <key>} или null
        "identity": spec.get("identity"),
    }


def check(
    installation: Installation, *, env: dict[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """Ошибки и предупреждения без обращения к стенду."""
    errors: list[str] = []
    warnings: list[str] = []
    validator = _schema_validator()
    domain = _domain()
    if domain is None:
        warnings.append(CORE_MISSING)

    for package in installation.packages:
        manifest = {
            "apiVersion": API_VERSION,
            "kind": "Package",
            "key": package.key,
            "spec": package.spec,
        }
        for error in validator.iter_errors(manifest):
            errors.append(f"{_rel(package.path / 'package.yaml')}: {_format_schema_error(error)}")

    seen: dict[tuple[str, str], Obj] = {}
    placeholder_env = dict(env or {})
    retired_agents = set(installation.retire.get("Agent") or [])
    for obj in installation.objects:
        where = _rel(obj.path)
        doc = {"apiVersion": API_VERSION, "kind": obj.kind, "key": obj.key, "spec": obj.spec}
        schema_errors = [_format_schema_error(e) for e in validator.iter_errors(doc)]
        errors.extend(f"{where}: {message}" for message in schema_errors)
        identity = (obj.kind, obj.ref)
        if identity in seen:
            errors.append(f"{where}: {obj.ref} уже объявлен в {_rel(seen[identity].path)}")
        seen[identity] = obj
        if schema_errors:
            continue
        owner = next(p for p in installation.packages if p.key == obj.package)
        spec = substitute(
            obj.spec,
            package_env(owner, placeholder_env),
            missing=lambda _name: "http://env.invalid",
        )
        if (
            domain is not None
            and obj.kind == "TaskType"
            and spec.get("contextSchema") is not None
            and domain.context_schema is None
        ):
            warnings.append(
                f"{where}: contextSchema не проверен — модуль профиля контекста control-plane "
                "не импортируется (релиз старше CP-ADR-0064 или нет зависимости regex); "
                "проверит ядро при публикации"
            )
        if domain is not None:
            try:
                _domain_check(obj, spec, domain)
            except domain.DomainError as error:
                details = f" {error.details}" if error.details else ""
                errors.append(f"{where}: {error.code}: {error.message}{details}")
            except PackageError as error:
                errors.append(f"{where}: {error}")

        # Замкнутость ссылок: только свой пакет и его requires (TAI-ADR-0044 п.3).
        visible = installation.visible(obj.package)
        task_types = {o.key for o in visible if o.kind == "TaskType"} | {SYSTEM_TASK_TYPE}
        skills = {f"{o.key}@{o.spec.get('version')}" for o in visible if o.kind == "Skill"}
        workspace_types = {o.key for o in visible if o.kind == "WorkspaceType"}
        artifact_types = {o.key: o for o in visible if o.kind == "ArtifactType"}
        agents = {o.key for o in visible if o.kind == "Agent"}

        def agent_ref(key: str, field: str) -> None:
            """Ссылка на агента — описание Agent своего пакета или его requires (как у остальных
            ссылок); выведенный из оборота этой же установкой — тоже ошибка (ядро: unknown_agent)."""
            if key in retired_agents:
                errors.append(
                    f"{where}: {field} ссылается на агента {key!r}, которого установка выводит "
                    "из оборота (retire)"
                )
            elif key not in agents:
                errors.append(
                    f"{where}: {field} ссылается на агента {key!r} — такого Agent нет "
                    f"в пакете {obj.package} и его requires"
                )

        def assignee_ref(value: Any, field: str) -> None:
            # agent:<key> литералом; шаблон ({{…}}, $.path, ${…}) разрешит ядро при исполнении
            if isinstance(value, str) and "{{" not in value and "$" not in value:
                match = AGENT_REF.match(value)
                if match:
                    agent_ref(match.group(1), field)
                elif value.startswith("agent:"):
                    errors.append(
                        f"{where}: {field} {value!r} — ссылка на агента пишется agent:<key> "
                        "(ключ — slug агента)"
                    )

        if obj.kind == "TaskType":
            errors.extend(
                f"{where}: {message}"
                for message in _artifact_schema_refs(
                    obj.spec.get("artifactSchema") or {}, artifact_types, obj.package
                )
            )
            execution = obj.spec.get("execution")
            if execution and f"{execution.get('skill')}@{execution.get('version')}" not in skills:
                errors.append(
                    f"{where}: execution ссылается на Skill {execution.get('skill')}@{execution.get('version')}, "
                    f"которого нет ни в пакете {obj.package}, ни в его requires"
                )
            for name, inputs in _outcome_actions(
                obj.spec.get("approvalSchema") or {}
            ) + _completion_actions(obj.spec.get("completionSchema") or {}):
                if (
                    name == "ensureWork"
                    and _literal(inputs.get("type"))
                    and inputs["type"] not in task_types
                ):
                    errors.append(
                        f"{where}: ensureWork.type {inputs['type']!r} — такого TaskType нет "
                        f"в пакете {obj.package} и его requires"
                    )
                if (
                    name == "invokeSkill"
                    and _literal(inputs.get("skill"))
                    and inputs["skill"] not in skills
                ):
                    errors.append(
                        f"{where}: invokeSkill.skill {inputs['skill']!r} — такого Skill (name@version) "
                        f"нет в пакете {obj.package} и его requires"
                    )
                if name == "ensureWork":
                    assignee_ref(inputs.get("assignee"), "ensureWork.assignee")
        if obj.kind == "Agent":
            errors.extend(
                f"{where}: {message}" for message in _working_copy_catalog_errors(obj.spec)
            )
            roles = {o.key for o in visible if o.kind == "Role"}
            for role in (obj.spec.get("identity") or {}).get("roles") or []:
                if role not in roles:
                    warnings.append(
                        f"{where}: роль {role!r} не объявлена в пакетах — должна уже быть в tenant"
                    )
            for task_type in (obj.spec.get("work") or {}).get("taskTypes") or []:
                if task_type not in task_types:
                    errors.append(
                        f"{where}: work.taskTypes {task_type!r} — такого TaskType нет "
                        f"в пакете {obj.package} и его requires"
                    )
        if obj.kind == "WorkRule":
            skill = (obj.spec.get("interpretation") or {}).get("skill")
            if skill and skill not in skills:
                errors.append(
                    f"{where}: interpretation.skill {skill!r} — такого Skill (name@version) "
                    f"нет в пакете {obj.package} и его requires"
                )
            action = obj.spec.get("action") or {}
            task_type = action.get("taskType")
            # шаблон {{item.…}} допустим только рядом с taskTypes (CP-ADR-0063 Г2) — ссылки тогда
            # проверяются по списку, а сам шаблон рендерит ядро
            if _literal(task_type) and "{{" not in task_type and task_type not in task_types:
                errors.append(
                    f"{where}: action.taskType {task_type!r} — такого TaskType нет "
                    f"в пакете {obj.package} и его requires"
                )
            for allowed in action.get("taskTypes") or []:
                if allowed not in task_types:
                    errors.append(
                        f"{where}: action.taskTypes {allowed!r} — такого TaskType нет "
                        f"в пакете {obj.package} и его requires"
                    )
            rule_identity = obj.spec.get("identity")
            if isinstance(rule_identity, dict) and isinstance(rule_identity.get("agent"), str):
                agent_ref(rule_identity["agent"], "identity.agent")
            assignee_ref(
                ((obj.spec.get("action") or {}).get("fields") or {}).get("assignee"),
                "action.fields.assignee",
            )
        if obj.kind == "WorkspaceType":
            for child in obj.spec.get("allowedChildTypes") or []:
                if child not in workspace_types:
                    warnings.append(
                        f"{where}: allowedChildTypes {child!r} не объявлен в пакетах — "
                        "должен уже быть в tenant"
                    )
        if obj.kind == "Process":
            calendar_objects = [o for o in visible if o.kind == "Calendar"]
            process_errors, process_warnings = _process_refs(
                obj.spec,
                task_types=task_types,
                skills=skills,
                calendars={o.key for o in calendar_objects},
                calendars_with_hours={
                    o.key for o in calendar_objects if o.spec.get("workingHours") is not None
                },
                package=obj.package,
            )
            errors.extend(f"{where}: {message}" for message in process_errors)
            warnings.extend(f"{where}: {message}" for message in process_warnings)
            agent = (obj.spec.get("identity") or {}).get("agent")
            if isinstance(agent, str):
                agent_ref(agent, "identity.agent")
            for candidate in obj.spec.get("owner") or []:
                if isinstance(candidate, dict) and isinstance(candidate.get("agent"), str):
                    agent_ref(candidate["agent"], "owner.agent")

    errors.extend(_test_errors(installation))
    manifest_errors, manifest_warnings = check_manifest(installation)
    errors.extend(manifest_errors)
    warnings.extend(manifest_warnings)
    for package in installation.packages:
        for rename in package.renames:
            if rename.get("kind") not in CATALOG_KINDS:
                errors.append(
                    f"{_rel(package.path / 'package.yaml')}: renames: вид {rename.get('kind')!r} "
                    "не объект каталога"
                )
            elif not any(
                o.kind == rename["kind"] and o.key == rename.get("to") for o in package.objects
            ):
                errors.append(
                    f"{_rel(package.path / 'package.yaml')}: renames: {rename['kind']}/{rename.get('to')} "
                    "— такого объекта в пакете нет"
                )

    known = {(o.kind, o.key) for o in installation.objects}
    for kind, keys in installation.retire.items():
        if kind not in RETIRABLE:
            errors.append(
                f"retire: вид {kind} не выводится из оборота (только {', '.join(RETIRABLE)})"
            )
            continue
        for key in keys:
            if kind == "TaskType" and key == SYSTEM_TASK_TYPE:
                errors.append(
                    "retire: системный тип task вывести нельзя — ядро держит его активную версию"
                )
            if (kind, key) in known:
                errors.append(
                    f"retire: {kind}/{key} одновременно объявлен в пакете и выводится из оборота"
                )
    return errors, warnings


def process_elements(spec: Any, path: str = "spec") -> list[tuple[str, str]]:
    """(id, путь) элементов процесса: стадии, шаги любой вложенности, вехи, таймеры, ветви
    fork, таблицы решений (их входы и выходы — столбцы, не элементы)."""
    skip = {
        "data",
        "form",
        "memory",
        "input",
        "set",
        "output",
        "export",
        "migrations",
        "governedBy",
        "retrospective",
        "context",
    }
    found: list[tuple[str, str]] = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in skip:
                    continue
                if key == "decisions" and isinstance(value, list):
                    found.extend(
                        (t["id"], f"{where}.decisions[{i}]")
                        for i, t in enumerate(value)
                        if isinstance(t, dict) and isinstance(t.get("id"), str)
                    )
                    continue
                walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    found.append((item["id"], f"{where}[{index}]"))
                walk(item, f"{where}[{index}]")

    walk(spec, path)
    return found


def _process_steps(spec: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), str):
                steps.append(node)
            for key, value in node.items():
                if key not in ("data", "form", "memory"):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(spec.get("stages"))
    walk(spec.get("onEvent"))
    walk(spec.get("timers"))
    walk(spec.get("correlate"))
    return steps


# Шаги, у которых бывает срок (due): processDue схемы.
DUE_STEPS = ("human", "approve", "call", "recall", "listen")
# Единицы срока, которые считает календарь.
WORKING_UNITS = ("workdays", "workhours")
# Ключ expect.sla теста для срока процесса целиком (spec.due).
SLA_PROCESS = "process"


def _process_dues(spec: dict[str, Any]) -> list[tuple[str, Any]]:
    """(где, due) процесса: spec.due и сроки шагов любой вложенности."""
    dues: list[tuple[str, Any]] = []
    if spec.get("due") is not None:
        dues.append(("spec.due", spec["due"]))
    for step in _process_steps(spec):
        for verb in DUE_STEPS:
            body = step.get(verb)
            if isinstance(body, dict) and body.get("due") is not None:
                dues.append((f"шаг {step.get('id')}: {verb}.due", body["due"]))
    return dues


def _due_calendar_refs(
    spec: dict[str, Any], calendars: set[str], calendars_with_hours: set[str]
) -> tuple[list[str], list[str]]:
    """Сроки в рабочих единицах считает календарь: due.calendar или spec.calendar процесса.

    Как у ядра при публикации: рабочих единиц без календаря нет, рабочие часы — только у
    календаря с workingHours. Календаря нет в пакете и его requires — предупреждение, как
    у spec.calendar: он может уже быть в tenant, а его рабочие часы проверит ядро."""
    errors: list[str] = []
    warnings: list[str] = []
    for where, due in _process_dues(spec):
        if not isinstance(due, dict):
            continue
        spans = [due]
        if isinstance(due.get("warnBefore"), dict):
            spans.append(due["warnBefore"])
        units = {unit for span in spans for unit in WORKING_UNITS if unit in span}
        own = due.get("calendar")
        if isinstance(own, str) and own not in calendars:
            warnings.append(
                f"{where}: calendar {own!r} не объявлен в пакете и его requires — должен уже "
                "быть в tenant"
            )
        if not units:
            continue
        key = own if own is not None else spec.get("calendar")
        if key is None:
            errors.append(
                f"{where}: {' и '.join(sorted(units))} считаются по календарю, а календаря нет — "
                "задайте spec.calendar процесса или calendar срока"
            )
        elif "workhours" in units and key in calendars and key not in calendars_with_hours:
            errors.append(
                f"{where}: workhours считаются по рабочим часам календаря, а у календаря "
                f"{key!r} нет workingHours — объявите их или считайте срок в workdays"
            )
    return errors, warnings


def _process_refs(
    spec: dict[str, Any],
    *,
    task_types: set[str],
    skills: set[str],
    calendars: set[str],
    package: str,
    calendars_with_hours: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}
    for element_id, where in process_elements(spec):
        if element_id in seen:
            errors.append(
                f"{where}: id {element_id!r} уже занят ({seen[element_id]}) — id элемента уникален в процессе"
            )
        else:
            seen[element_id] = where
    tables = {t.get("id") for t in spec.get("decisions") or []}
    for step in _process_steps(spec):
        sid = step.get("id")
        for verb in ("human", "approve"):
            task_type = (
                (step.get(verb) or {}).get("taskType") if isinstance(step.get(verb), dict) else None
            )
            if isinstance(task_type, str) and task_type not in task_types:
                errors.append(
                    f"шаг {sid}: {verb}.taskType {task_type!r} — такого TaskType нет "
                    f"в пакете {package} и его requires"
                )
        call = step.get("call")
        if (
            isinstance(call, dict)
            and isinstance(call.get("skill"), str)
            and call["skill"] not in skills
        ):
            errors.append(
                f"шаг {sid}: call.skill {call['skill']!r} — такого Skill (name@version) нет "
                f"в пакете {package} и его requires"
            )
        decide = step.get("decide")
        if isinstance(decide, dict) and decide.get("table") not in tables:
            errors.append(
                f"шаг {sid}: decide.table {decide.get('table')!r} — такой таблицы нет в spec.decisions"
            )
        compensate = step.get("compensate")
        if isinstance(compensate, list):
            for target in compensate:
                if target not in seen:
                    errors.append(
                        f"шаг {sid}: compensate {target!r} — такого элемента в процессе нет"
                    )
    retrospective = spec.get("retrospective") or {}
    if (
        isinstance(retrospective.get("taskType"), str)
        and retrospective["taskType"] not in task_types
    ):
        errors.append(
            f"retrospective.taskType {retrospective['taskType']!r} — такого TaskType нет "
            f"в пакете {package} и его requires"
        )
    if not spec.get("owner"):
        # владелец процесса (TAI-ADR-0054 п.5, амендмент 2026-09-27): кому адресовать задачи о
        # процессе — расхождение с регламентом (regulation-drift), ошибки экземпляров
        warnings.append(
            "owner не задан — задачам о процессе (расхождение с регламентом, ошибки "
            "экземпляров) некому адресоваться"
        )
    calendar = spec.get("calendar")
    if isinstance(calendar, str) and calendar not in calendars:
        warnings.append(
            f"calendar {calendar!r} не объявлен в пакете и его requires — должен уже быть в tenant"
        )
    due_errors, due_warnings = _due_calendar_refs(
        spec, calendars, calendars if calendars_with_hours is None else calendars_with_hours
    )
    errors.extend(due_errors)
    warnings.extend(due_warnings)
    version = spec.get("version")
    for migration in spec.get("migrations") or []:
        for target in (migration.get("map") or {}).values():
            if migration.get("to") == version and target not in seen:
                errors.append(
                    f"migrations {migration.get('from')}→{migration.get('to')}: {target!r} — такого "
                    "элемента в процессе нет"
                )
    return errors, warnings


def _test_validator() -> Any:
    return schema_module.validator(schema_module.TEST)


# Предмет теста (subject, CP-ADR-0074 Z1): поле с ключом объекта и его вид.
TEST_SUBJECTS = {
    "process": ("process", "Process"),
    "rule": ("rule", "WorkRule"),
    "taskType": ("taskType", "TaskType"),
}


# Байты одного given.artifacts[].content в UTF-8, как у ядра (GIVEN_CONTENT_MAX_BYTES,
# CP-ADR-0074 Z8): схема считает символы, а символ — до четырёх байт.
GIVEN_CONTENT_MAX_BYTES = 1024 * 1024


def _given_content_errors(data: dict[str, Any]) -> list[str]:
    """Содержимое артефактов given теста — не больше 1 МиБ в UTF-8 (ядро откажет given_refused)."""
    given = data.get("given")
    artifacts = given.get("artifacts") if isinstance(given, dict) else None
    errors = []
    for index, artifact in enumerate(artifacts if isinstance(artifacts, list) else ()):
        content = artifact.get("content") if isinstance(artifact, dict) else None
        if not isinstance(content, str):
            continue
        size = len(content.encode("utf-8"))
        if size > GIVEN_CONTENT_MAX_BYTES:
            errors.append(
                f"given.artifacts[{index}].content: {size} байт в UTF-8 — больше "
                f"{GIVEN_CONTENT_MAX_BYTES} (1 МиБ), ядро такой given отвергнет"
            )
    return errors


def _test_errors(installation: Installation) -> list[str]:
    """Тесты пакета: форма по test.schema.json, предмет — объект своего пакета (процесс,
    правило или тип задачи), шаги теста процесса — его элементы."""
    errors: list[str] = []
    if not installation.tests:
        return errors
    validator = _test_validator()
    objects = {(o.package, o.kind, o.key): o for o in installation.objects}
    for test in installation.tests:
        where = _rel(test.path)
        schema_errors = [_format_schema_error(e) for e in validator.iter_errors(test.data)]
        errors.extend(f"{where}: {message}" for message in schema_errors)
        if schema_errors:
            continue
        errors.extend(f"{where}: {message}" for message in _given_content_errors(test.data))
        field, kind = TEST_SUBJECTS[test.data.get("subject") or "process"]
        subject = objects.get((test.package, kind, test.data[field]))
        if subject is None:
            errors.append(
                f"{where}: {field} {test.data[field]!r} — такого {kind} нет в пакете {test.package}"
            )
            continue
        if kind != "Process":
            continue
        process = subject
        elements = {element_id for element_id, _ in process_elements(process.spec)}
        with_due = {
            step.get("id")
            for step in _process_steps(process.spec)
            if any(
                isinstance(step.get(verb), dict) and step[verb].get("due") is not None
                for verb in DUE_STEPS
            )
        }
        for index, step in enumerate(test.data.get("steps") or []):
            for verb in ("complete", "approve"):
                target = (step.get(verb) or {}).get("step")
                if target is not None and target not in elements:
                    errors.append(
                        f"{where}: steps[{index}].{verb}.step {target!r} — такого шага в процессе "
                        f"{process.key} нет"
                    )
            # expect.sla: id шага — срок его открытой попытки, process — срок процесса
            for target in (step.get("expect") or {}).get("sla") or {}:
                if target == SLA_PROCESS:
                    if process.spec.get("due") is None:
                        errors.append(
                            f"{where}: steps[{index}].expect.sla.process — у процесса "
                            f"{process.key} нет spec.due"
                        )
                elif target not in elements:
                    errors.append(
                        f"{where}: steps[{index}].expect.sla {target!r} — такого шага в процессе "
                        f"{process.key} нет"
                    )
                elif target not in with_due:
                    errors.append(
                        f"{where}: steps[{index}].expect.sla {target!r} — у этого элемента "
                        f"процесса {process.key} нет срока (due)"
                    )
    return errors


def _artifact_schema_refs(
    schema: dict[str, Any], artifact_types: dict[str, Obj], package: str
) -> list[str]:
    """Входы и выходы ссылаются на ArtifactType своего пакета или его requires; сужение
    mediaTypes слота — подмножество mediaTypes типа (ядро проверит то же при публикации)."""
    domain = _domain()
    errors = []
    for side in ("inputs", "outputs"):
        for slot in schema.get(side) or []:
            declared = artifact_types.get(slot.get("type"))
            if declared is None:
                errors.append(
                    f"artifactSchema.{side} {slot.get('key')!r}: тип артефакта {slot.get('type')!r} "
                    f"не объявлен ни в пакете {package}, ни в его requires"
                )
                continue
            narrowed = slot.get("mediaTypes")
            if narrowed and domain is not None and domain.artifact_type is not None:
                allowed = [m.strip().lower() for m in declared.spec.get("mediaTypes") or ["*/*"]]
                wide = [
                    m
                    for m in narrowed
                    if not domain.artifact_type.pattern_covered(allowed, m.strip().lower())
                ]
                if wide:
                    errors.append(
                        f"artifactSchema.{side} {slot.get('key')!r}: mediaTypes {wide} шире, "
                        f"чем у типа {declared.key} ({allowed})"
                    )
    return errors
