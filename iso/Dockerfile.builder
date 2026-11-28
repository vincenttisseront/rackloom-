# Image Linux de reconditionnement ISO (xorriso, gpg, apt).
# Utilisee par iso/build.ps1 sous Windows via Docker Desktop (Linux containers).
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

# Entrypoint figé en LF dans l'image (indépendant du checkout Windows).
COPY iso/docker-entrypoint.sh /usr/local/bin/rackloom-iso-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/rackloom-iso-entrypoint \
  && chmod 0755 /usr/local/bin/rackloom-iso-entrypoint

WORKDIR /src
ENTRYPOINT ["/usr/local/bin/rackloom-iso-entrypoint"]
CMD []
