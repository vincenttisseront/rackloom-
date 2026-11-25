# syntax=docker/dockerfile:1
# Launcher : seul composant qui voit le socket du moteur de conteneurs. Aucun réseau, aucun accès base.
# Contexte de construction : racine du dépôt.

FROM python:3.12-slim-bookworm AS build
ARG UV_VERSION=0.13.10
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /src/launcher
COPY launcher/pyproject.toml launcher/uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
 && python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt
COPY launcher/src ./src
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

FROM python:3.12-slim-bookworm
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["rackloom-launcher", "--uds", "/run/rackloom/launcher.sock"]
