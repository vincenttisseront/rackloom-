# syntax=docker/dockerfile:1
# Image unique de l'API et du worker (deux points d'entrée). Contexte de construction : racine du dépôt.
# Dépendances installées depuis uv.lock avec vérification des hachages ; utilisateur non root 10001.

FROM python:3.12-slim-bookworm AS build
ARG UV_VERSION=0.13.10
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /src/backend
COPY backend/pyproject.toml backend/uv.lock ./
COPY launcher/pyproject.toml /src/launcher/pyproject.toml
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
 && python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt
COPY backend/src ./src
COPY backend/README.md* ./
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

FROM python:3.12-slim-bookworm
RUN groupadd -g 10001 rackloom \
 && useradd -u 10001 -g 10001 -M -d /nonexistent -s /usr/sbin/nologin rackloom
COPY --from=build /opt/venv /opt/venv
COPY backend/alembic /opt/rackloom/alembic
COPY catalog /opt/rackloom/catalog
COPY lab/fixtures /opt/rackloom/lab/fixtures
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RACKLOOM_ALEMBIC_DIR=/opt/rackloom/alembic
WORKDIR /opt/rackloom
USER 10001:10001
CMD ["uvicorn", "rackloom.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
