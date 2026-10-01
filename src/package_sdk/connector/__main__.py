"""Единая точка входа образа наблюдателя: ``python -m package_sdk.connector``.

Процесс читает ревизию своего агента, берёт ``executor.params.entrypoint`` (у
собственного вида исполнителя — ``CONNECTOR_ENTRYPOINT`` образа), импортирует
наблюдателя из образа и крутит его цикл. Точки входа нет в образе или она не помечена
@observer — выход 2: узел не перезапускает процесс впустую.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from package_sdk.connector.runtime import (
    DEFAULT_DATA_DIR,
    DEFAULT_SECRETS_DIR,
    ENV_DATA_DIR,
    ENV_ENTRYPOINT,
    ENV_SECRETS_DIR,
    EXIT_MISCONFIGURED,
    EXIT_STOPPED,
    AgentRevision,
    Runner,
    _agent_credentials,
    load_entrypoint,
)

logger = logging.getLogger("package_sdk.connector")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    secrets_dir = Path(os.environ.get(ENV_SECRETS_DIR, DEFAULT_SECRETS_DIR))
    os.environ.update(_agent_credentials(os.environ, secrets_dir))
    server = os.environ.get("CONTROL_PLANE_SERVER")
    if not server:
        logger.error("CONTROL_PLANE_SERVER is not set: the node passes it to the agent")
        return EXIT_MISCONFIGURED
    from package_sdk.connector.core import ClientCore

    core = ClientCore(server)
    body = asyncio.run(core.get_my_agent())
    if not body:
        logger.error("the principal is not bound to an agent: nothing describes what to observe")
        return EXIT_MISCONFIGURED
    revision = AgentRevision.from_body(body)
    if revision.inactive:
        return EXIT_STOPPED
    try:
        # собственный вид исполнителя (git-connector) точки входа в params не несёт —
        # её задаёт образ переменной CONNECTOR_ENTRYPOINT
        entrypoint = revision.params.get("entrypoint") or os.environ.get(ENV_ENTRYPOINT) or ""
        function = load_entrypoint(str(entrypoint))
    except ValueError as exc:
        logger.error("%s: %s", revision.label, exc)
        return EXIT_MISCONFIGURED
    runner = Runner(
        function,
        core,
        data_dir=Path(os.environ.get(ENV_DATA_DIR, DEFAULT_DATA_DIR)),
        secrets_dir=secrets_dir,
        run_async=asyncio.run,
    )
    return runner.serve()


if __name__ == "__main__":
    sys.exit(main())
