"""Среда наблюдателя пакета интеграции (plan Р8; extra ``connector``).

from package_sdk.connector import Observation, ObserveContext, observer, run

@observer(kind="helpdesk-observer", entrypoint="acme_helpdesk.agent:observe")
def observe(ctx: ObserveContext) -> None:
    ...

if __name__ == "__main__":
    run(observe)
"""

from package_sdk.connector.runtime import (
    CYCLE_FAILED,
    EXIT_MISCONFIGURED,
    EXIT_REVISION_CHANGED,
    EXIT_STOPPED,
    SECRET_MISSING,
    Document,
    Observation,
    ObserveContext,
    PublishError,
    Registered,
    SecretMissing,
    Snapshot,
    load_entrypoint,
    observer,
    run,
)
from package_sdk.connector.secrets import SecretRejected

__all__ = [
    "CYCLE_FAILED",
    "EXIT_MISCONFIGURED",
    "EXIT_REVISION_CHANGED",
    "EXIT_STOPPED",
    "SECRET_MISSING",
    "Document",
    "Observation",
    "ObserveContext",
    "PublishError",
    "Registered",
    "SecretMissing",
    "SecretRejected",
    "Snapshot",
    "load_entrypoint",
    "observer",
    "run",
]
