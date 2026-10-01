# syntax=docker/dockerfile:1.7
# The base image of observers for the stand job — what the platform ships as its observer
# base: Python, the core client and package-sdk[connector], installed from the checkouts
# of the components, never from the public index. `package-sdk image observer` puts the
# integration of a package on top of it.
#
#   docker build -f observer-base.Dockerfile \
#     --build-context package-sdk=<package-sdk> --build-context client=<control-plane>/client \
#     -t claims-stand/observer-base:ci .
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=client pyproject.toml README.md /opt/platform/control-plane-client/
COPY --from=client src /opt/platform/control-plane-client/src
COPY --from=package-sdk pyproject.toml README.md /opt/platform/package-sdk/
COPY --from=package-sdk src /opt/platform/package-sdk/src
COPY --from=package-sdk schema /opt/platform/package-sdk/schema
RUN pip install --no-cache-dir /opt/platform/control-plane-client \
      "/opt/platform/package-sdk[connector]" \
 && python -c "import package_sdk.connector, control_plane_client"
