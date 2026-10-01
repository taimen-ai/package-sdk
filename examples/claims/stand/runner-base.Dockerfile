# syntax=docker/dockerfile:1.7
# The base image of skills hosts for the stand job — what the platform ships as its
# executor image, without coding agents: the control-plane-agent daemon, skill-sdk with
# ctx.llm and the core client, installed from the checkouts of the components.
# `package-sdk image skills` puts the integration of a package on top of it.
#
#   docker build -f runner-base.Dockerfile \
#     --build-context control-plane=<control-plane> --build-context auth=<platform-auth-sdk> \
#     --build-context skill-sdk=<skill-sdk> --build-context llm=<platform-llm> \
#     -t claims-stand/runner-base:ci .
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_LINK_MODE=copy
# uv: the skills image of a package installs its integration with it
RUN pip install --no-cache-dir uv
# Only what the builds need: the project files and the sources — never .git or tests.
COPY --from=auth pyproject.toml README.md /opt/platform/platform-auth-sdk/
COPY --from=auth src /opt/platform/platform-auth-sdk/src
COPY --from=llm pyproject.toml README.md /opt/platform/platform-llm/
COPY --from=llm src /opt/platform/platform-llm/src
COPY --from=skill-sdk pyproject.toml README.md /opt/platform/skill-sdk/
COPY --from=skill-sdk src /opt/platform/skill-sdk/src
COPY --from=control-plane pyproject.toml README.md /opt/platform/control-plane/
COPY --from=control-plane client /opt/platform/control-plane/client
COPY --from=control-plane src /opt/platform/control-plane/src
# The executor's environment is a virtual environment, as in the executor image of the
# platform: `package-sdk image skills` installs the integration into it.
ENV VIRTUAL_ENV=/opt/platform/venv PATH=/opt/platform/venv/bin:$PATH
RUN uv venv /opt/platform/venv \
 && uv pip install --no-cache /opt/platform/platform-auth-sdk \
      /opt/platform/platform-llm /opt/platform/control-plane/client /opt/platform/control-plane \
      "/opt/platform/skill-sdk[llm]" \
 && python -c "import skill_sdk, control_plane_client, control_plane_agent"
# The agent's PAT is the node's file /run/secrets/agent-pat; it lives only in the
# environment of the daemon, never in the image.
COPY --chmod=0755 <<'SCRIPT' /usr/local/bin/skills-host
#!/bin/sh
set -eu
IAM_PLATFORM_ACCESS_TOKEN="$(tr -d '\r\n' < /run/secrets/agent-pat)"
export IAM_PLATFORM_ACCESS_TOKEN IAM_CREDENTIAL_MODE=environment IAM_NO_KEYCHAIN=1
exec control-plane-agent
SCRIPT
ENV HOME=/tmp/skills-host CONTROL_PLANE_AGENT_CONFIG=revision
ENTRYPOINT ["skills-host"]
