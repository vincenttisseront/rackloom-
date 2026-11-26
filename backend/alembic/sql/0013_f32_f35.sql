-- F33 : sorties de déploiement typées et versionnées (allowlist, jamais le state).
-- F34 : quotas de licences pour la source fictive locale.
-- F35 : identités de service (jetons Bearer limités à un projet).

CREATE TABLE deployment_outputs (
    id uuid PRIMARY KEY,
    deployment_id uuid NOT NULL REFERENCES deployments(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    key text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{0,40}$'),
    output_type text NOT NULL CHECK (output_type IN ('name', 'vm_uuid', 'fqdn', 'service_id')),
    value text NOT NULL CHECK (char_length(value) <= 500),
    version integer NOT NULL CHECK (version > 0),
    instance_name text,
    published_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (deployment_id, key, instance_name, version)
);
CREATE INDEX deployment_outputs_deployment ON deployment_outputs (deployment_id, version DESC);
CREATE INDEX deployment_outputs_project ON deployment_outputs (project_id);

CREATE TABLE license_quotas (
    sku text PRIMARY KEY CHECK (sku ~ '^[a-z][a-z0-9_.-]{0,60}$'),
    available integer NOT NULL CHECK (available >= 0),
    reachable boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE users
    ADD COLUMN kind text NOT NULL DEFAULT 'human'
        CHECK (kind IN ('human', 'service'));

CREATE TABLE service_tokens (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    token_hash text NOT NULL UNIQUE,
    project_id uuid NOT NULL REFERENCES projects(id),
    label text NOT NULL CHECK (char_length(label) <= 120),
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    last_used_at timestamptz
);
CREATE INDEX service_tokens_user ON service_tokens (user_id) WHERE revoked_at IS NULL;
CREATE INDEX service_tokens_project ON service_tokens (project_id) WHERE revoked_at IS NULL;

-- Rôles Compose (api / worker) : sans GRANT, publish F33 fait échouer tf_apply après envoi → uncertain + disjoncteur.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT ON deployment_outputs TO rackloom_api;
        GRANT SELECT ON license_quotas TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON service_tokens TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT, INSERT ON deployment_outputs TO rackloom_worker;
        GRANT SELECT ON license_quotas TO rackloom_worker;
        GRANT SELECT ON service_tokens TO rackloom_worker;
    END IF;
END $$;
