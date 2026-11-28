# Image Linux de reconditionnement ISO (xorriso, gpg, apt).
# Utilisée par iso/build.ps1 sous Windows via Docker Desktop (Linux containers).
# Ne contient aucun secret ; pas de socket Docker.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gpg \
    gpg-agent \
    xorriso \
    cpio \
    genisoimage \
    squashfs-tools \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# build.sh est monté depuis le dépôt ; pas d'entrypoint figé.
CMD ["bash", "iso/build.sh"]
