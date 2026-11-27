-- Lot C : F10 politiques/exceptions, F11 droits temporaires, F12 réauth, F13 admin déléguée, F14 urgence.

-- F10 : exceptions de politique (propriétaire, périmètre, expiration).
CREATE TABLE policy_exceptions (
    id uuid PRIMARY KEY,
    rule_id text NOT NULL CHECK (char_length(rule_id) BETWEEN 2 AND 40),
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    owner_user_id uuid NOT NULL REFERENCES users(id),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
    expires_at timestamptz NOT NULL,
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    revoked_by uuid REFERENCES users(id),
    revoke_reason text CHECK (revoke_reason IS NULL OR length(btrim(revoke_reason)) BETWEEN 3 AND 500),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL) AND (revoked_at IS NULL) = (revoke_reason IS NULL))
);
CREATE INDEX policy_exceptions_scope ON policy_exceptions (project_id, environment_id, rule_id)
    WHERE revoked_at IS NULL;
CREATE INDEX policy_exceptions_expiry ON policy_exceptions (expires_at) WHERE revoked_at IS NULL;

-- F11 : droits temporaires à la demande (projet, environnement, opération, durée).
CREATE TABLE temporary_grants (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL CHECK (role IN ('operator', 'approver', 'auditor', 'project_admin', 'connector_admin')),
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    operation text NOT NULL DEFAULT 'any'
        CHECK (operation IN ('any', 'approve', 'deploy', 'configure_connector', 'manage_policy')),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
    granted_by uuid NOT NULL REFERENCES users(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_by uuid REFERENCES users(id),
    revoke_reason text CHECK (revoke_reason IS NULL OR length(btrim(revoke_reason)) BETWEEN 3 AND 500),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL) AND (revoked_at IS NULL) = (revoke_reason IS NULL)),
    CHECK (expires_at > granted_at)
);
CREATE INDEX temporary_grants_user ON temporary_grants (user_id) WHERE revoked_at IS NULL;
CREATE INDEX temporary_grants_expiry ON temporary_grants (expires_at) WHERE revoked_at IS NULL;

-- F12 : niveau d'authentification (acr) et preuve de step-up sur la session.
ALTER TABLE sessions
    ADD COLUMN acr text NOT NULL DEFAULT 'pwd'
        CHECK (acr IN ('pwd', 'otp', 'mfa', 'urn:rackloom:step-up')),
    ADD COLUMN amr text[] NOT NULL DEFAULT ARRAY['pwd']::text[];

-- F13 : rôles d'administration déléguée par projet.
ALTER TABLE role_bindings DROP CONSTRAINT IF EXISTS role_bindings_role_check;
ALTER TABLE role_bindings
    ADD CONSTRAINT role_bindings_role_check
    CHECK (role IN (
        'platform_admin', 'template_publisher', 'template_author',
        'operator', 'approver', 'auditor',
        'project_admin', 'connector_admin'
    ));

-- F14 : interventions d'accès d'urgence (SSO indisponible).
CREATE TABLE emergency_interventions (
    id uuid PRIMARY KEY,
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
    activated_at timestamptz NOT NULL DEFAULT now(),
    activated_by text NOT NULL CHECK (char_length(activated_by) BETWEEN 1 AND 120),
    expires_at timestamptz NOT NULL,
    closed_at timestamptz,
    closed_by text,
    close_reason text CHECK (close_reason IS NULL OR length(btrim(close_reason)) BETWEEN 3 AND 500),
    sso_was_unavailable boolean NOT NULL DEFAULT true,
    CHECK ((closed_at IS NULL) = (closed_by IS NULL) AND (closed_at IS NULL) = (close_reason IS NULL)),
    CHECK (expires_at > activated_at)
);
CREATE UNIQUE INDEX emergency_interventions_active
    ON emergency_interventions ((true)) WHERE closed_at IS NULL;

CREATE TABLE emergency_allowlist (
    id uuid PRIMARY KEY,
    intervention_id uuid NOT NULL REFERENCES emergency_interventions(id),
    user_id uuid NOT NULL REFERENCES users(id),
    UNIQUE (intervention_id, user_id)
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON policy_exceptions TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON temporary_grants TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON emergency_interventions TO rackloom_api;
        GRANT SELECT, INSERT, DELETE ON emergency_allowlist TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON policy_exceptions TO rackloom_worker;
        GRANT SELECT ON temporary_grants TO rackloom_worker;
        GRANT SELECT ON emergency_interventions TO rackloom_worker;
        GRANT SELECT ON emergency_allowlist TO rackloom_worker;
    END IF;
END $$;
