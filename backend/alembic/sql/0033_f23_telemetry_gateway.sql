-- F23 : passerelle de télémétrie.
-- Agents enrôlés par mTLS, liés à un projet. L'identité authentifiée (empreinte)
-- prime sur toute identité déclarée dans le contenu. Quotas, rétention et limites
-- de taille par projet. Aucune écriture inter-projet.

CREATE TABLE telemetry_agents (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    project_id uuid NOT NULL REFERENCES projects(id),
    -- Identité mTLS : empreinte SHA-256 du certificat client (hex minuscule, 64).
    cert_fingerprint_sha256 text NOT NULL UNIQUE
        CHECK (cert_fingerprint_sha256 ~ '^[a-f0-9]{64}$'),
    cert_subject text NOT NULL CHECK (char_length(cert_subject) BETWEEN 1 AND 500),
    cert_not_before timestamptz NOT NULL,
    cert_not_after timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'enrolled'
        CHECK (status IN ('enrolled', 'active', 'revoked')),
    last_seen_at timestamptz,
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    enrolled_by uuid REFERENCES users(id),
    revoked_at timestamptz,
    revoked_by uuid REFERENCES users(id),
    revoke_reason text CHECK (revoke_reason IS NULL OR char_length(revoke_reason) BETWEEN 1 AND 500),
    CHECK (cert_not_after > cert_not_before),
    CHECK (status <> 'revoked' OR revoked_at IS NOT NULL)
);
CREATE INDEX telemetry_agents_project ON telemetry_agents (project_id) WHERE status <> 'revoked';
CREATE INDEX telemetry_agents_fingerprint ON telemetry_agents (cert_fingerprint_sha256)
    WHERE status <> 'revoked';

CREATE TABLE telemetry_project_limits (
    project_id uuid PRIMARY KEY REFERENCES projects(id),
    -- Taille max d'un échantillon (corps JSON sérialisé).
    max_payload_bytes integer NOT NULL DEFAULT 65536
        CHECK (max_payload_bytes BETWEEN 64 AND 1048576),
    -- Quota glissant : octets acceptés par fenêtre (plancher bas pour recettes de labo).
    quota_bytes_per_window bigint NOT NULL DEFAULT 1048576
        CHECK (quota_bytes_per_window BETWEEN 64 AND 1073741824),
    window_seconds integer NOT NULL DEFAULT 3600
        CHECK (window_seconds BETWEEN 60 AND 86400),
    retention_days integer NOT NULL DEFAULT 7
        CHECK (retention_days BETWEEN 1 AND 90),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE telemetry_usage_windows (
    project_id uuid NOT NULL REFERENCES projects(id),
    window_start timestamptz NOT NULL,
    bytes_accepted bigint NOT NULL DEFAULT 0 CHECK (bytes_accepted >= 0),
    samples_accepted integer NOT NULL DEFAULT 0 CHECK (samples_accepted >= 0),
    PRIMARY KEY (project_id, window_start)
);

CREATE TABLE telemetry_samples (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    agent_id uuid NOT NULL REFERENCES telemetry_agents(id),
    kind text NOT NULL CHECK (kind IN ('metrics', 'logs')),
    received_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    byte_size integer NOT NULL CHECK (byte_size > 0),
    -- Contenu accepté (jamais de secret attendu ; scrub à l'ingestion si canari).
    payload jsonb NOT NULL,
    -- Projet déclaré dans le corps (audit) ; jamais utilisé pour l'autorisation.
    declared_project text CHECK (declared_project IS NULL OR char_length(declared_project) <= 80),
    request_id text CHECK (request_id IS NULL OR char_length(request_id) <= 80)
);
CREATE INDEX telemetry_samples_project_received
    ON telemetry_samples (project_id, received_at DESC);
CREATE INDEX telemetry_samples_expires ON telemetry_samples (expires_at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON telemetry_agents TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON telemetry_project_limits TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON telemetry_usage_windows TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON telemetry_samples TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON telemetry_agents TO rackloom_worker;
        GRANT SELECT ON telemetry_project_limits TO rackloom_worker;
        GRANT SELECT ON telemetry_usage_windows TO rackloom_worker;
        GRANT SELECT, DELETE ON telemetry_samples TO rackloom_worker;
    END IF;
END $$;
