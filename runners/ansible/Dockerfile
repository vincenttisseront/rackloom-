# syntax=docker/dockerfile:1
# Runner Ansible : ansible-core et ansible-runner épinglés avec hachages, ansible.cfg figé,
# clé SSH reçue sur l'entrée standard et chargée dans un agent éphémère.
# Réseau : rn-lab interne (nœuds de labo seulement). Contexte de construction : racine du dépôt.

FROM python:3.12-slim-bookworm
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*
COPY runners/ansible/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN groupadd -g 10001 runner && useradd -u 10001 -g 10001 -M -d /nonexistent -s /usr/sbin/nologin runner
COPY runners/ansible/ansible.cfg /etc/ansible/ansible.cfg
COPY runners/common/rackloom_runner.py /opt/rackloom/rackloom_runner.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python3", "/opt/rackloom/rackloom_runner.py"]
