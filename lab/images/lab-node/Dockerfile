# Nœud de labo Rackloom : Ubuntu 24.04, OpenSSH et un dépôt apt local signé contenant Nginx,
# Fail2ban et nftables (avec leurs dépendances).
# Aucun accès réseau sortant à l'exécution (réseau rn-lab interne).
# Les clés d'hôte SSH sont générées au premier démarrage, jamais embarquées dans l'image.
#
# Hypothèse H4 du plan infirmée sur Docker sans --privileged : systemd ne peut pas créer
# son groupe de contrôle (/sys/fs/cgroup en lecture seule). Repli : PID 1 = tini,
# services gérés par leurs scripts SysV ; les contrôles systemd sont « non collectés ».

FROM ubuntu:24.04 AS repo
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends apt-utils gnupg ca-certificates dpkg-dev \
 && mkdir -p /srv/lab-repo && cd /srv/lab-repo \
 && apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
      --no-breaks --no-replaces --no-enhances nginx fail2ban nftables | grep '^\w' | sort -u) \
 && dpkg-scanpackages --multiversion . /dev/null > Packages && gzip -k9 Packages \
 && apt-ftparchive -o APT::FTPArchive::Release::Origin=rackloom-lab \
      -o APT::FTPArchive::Release::Label=rackloom-lab release . > Release \
 && export GNUPGHOME=$(mktemp -d) \
 && gpg --batch --passphrase '' --quick-gen-key "Rackloom lab repo (build only)" ed25519 sign never \
 && gpg --batch --yes --armor --detach-sign -o Release.gpg Release \
 && gpg --batch --yes --clearsign -o InRelease Release \
 && gpg --batch --export > /srv/rackloom-lab-repo.gpg \
 && rm -rf "$GNUPGHOME"

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server python3 python3-apt sudo tini ca-certificates \
      iproute2 procps \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/ssh/ssh_host_* \
 && rm -f /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list
COPY --from=repo /srv/lab-repo /srv/lab-repo
COPY --from=repo /srv/rackloom-lab-repo.gpg /usr/share/keyrings/rackloom-lab.gpg
RUN echo "deb [signed-by=/usr/share/keyrings/rackloom-lab.gpg] file:/srv/lab-repo ./" \
      > /etc/apt/sources.list.d/rackloom-lab.list \
 && useradd -m -s /bin/bash rackloom-collect \
 && useradd -m -s /bin/bash rackloom-config \
 && usermod -aG adm rackloom-collect \
 && echo "rackloom-config ALL=(root) NOPASSWD: ALL" > /etc/sudoers.d/rackloom-config \
 && chmod 0440 /etc/sudoers.d/rackloom-config \
 && install -d -m 0755 /run/sshd
COPY sshd-rackloom.conf /etc/ssh/sshd_config.d/10-rackloom.conf
COPY lab-node-init.sh /usr/local/sbin/lab-node-init.sh
RUN chmod 0755 /usr/local/sbin/lab-node-init.sh
EXPOSE 22 80
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/sbin/lab-node-init.sh"]
