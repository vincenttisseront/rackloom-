# syntax=docker/dockerfile:1
# WebUI (Nuxt 4 en mode SPA, fichiers statiques) servie par Caddy, terminaison TLS locale.
# Contexte de construction : racine du dépôt.

FROM node:22-bookworm-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund --ignore-scripts
COPY web/ ./
RUN npx nuxi generate && node scripts/csp.mjs .output/public /src/web/csp.caddy

FROM caddy:2.10.2-alpine
# Le binaire officiel porte cap_net_bind_service : refusé sous cap_drop ALL + no-new-privileges,
# et inutile puisque l'écoute se fait sur 8443.
RUN apk add --no-cache libcap \
    && setcap -r /usr/bin/caddy \
    && apk del libcap \
    && addgroup -g 10001 rackloom && adduser -D -H -u 10001 -G rackloom rackloom \
    && mkdir -p /data /config && chown 10001:10001 /data /config
COPY compose/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=web /src/web/.output/public /srv/web
COPY --from=web /src/web/csp.caddy /etc/caddy/csp.caddy
USER 10001:10001
