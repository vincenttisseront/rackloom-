# syntax=docker/dockerfile:1
# Runner Terraform : CLI épinglée (version et SHA-256 dans versions.lock), providers servis par un
# miroir local figé à la construction. Exécuté sans réseau (profil launcher « terraform »).
# Contexte de construction : racine du dépôt.

FROM python:3.12-slim-bookworm AS fetch
ARG TERRAFORM_VERSION=1.17.2
ARG TERRAFORM_SHA256=e6f16b2f0b137ee5729caa8c4e79a3bae4ee261acd198a1fcfa8584a6296605b
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSLo /tmp/terraform.zip \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
 && echo "${TERRAFORM_SHA256}  /tmp/terraform.zip" | sha256sum -c - \
 && unzip -d /usr/local/bin /tmp/terraform.zip terraform \
 && rm /tmp/terraform.zip
COPY catalog/terraform/lab-vm/1.0.0 /tmp/lab-vm
RUN cd /tmp/lab-vm && CHECKPOINT_DISABLE=1 terraform providers mirror -platform=linux_amd64 /opt/rackloom/providers

FROM python:3.12-slim-bookworm
RUN groupadd -g 10001 runner && useradd -u 10001 -g 10001 -M -d /nonexistent -s /usr/sbin/nologin runner
COPY --from=fetch /usr/local/bin/terraform /usr/local/bin/terraform
COPY --from=fetch /opt/rackloom/providers /opt/rackloom/providers
COPY runners/terraform/terraformrc /opt/rackloom/terraformrc
COPY runners/common/rackloom_runner.py /opt/rackloom/rackloom_runner.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TF_CLI_CONFIG_FILE=/opt/rackloom/terraformrc
USER 10001:10001
ENTRYPOINT ["python3", "/opt/rackloom/rackloom_runner.py"]
