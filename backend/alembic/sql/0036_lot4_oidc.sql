-- Lot 4 : SSO OIDC (Authlib code + PKCE), mapping groupes → rôles, états de flux.
-- Client secret chiffré avec la clé API `oidc-client` (jamais en clair, jamais journalisé).
-- IdP d'entreprise : non vérifié (D2) ; Dex local pour démo / e2e.

CREATE TABLE oidc_settings (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled boolean NOT NULL DEFAULT false,
    issuer_url text NOT NULL DEFAULT ''
        CHECK (issuer_url = '' OR issuer_url ~ '^https://' OR issuer_url ~ '^http://127\\.0\\.0\\.1' OR issuer_url ~ '^http://localhost'),
    client_id text NOT NULL DEFAULT ''
        CHECK (char_length(client_id) <= 200),
    -- AES-GCM (clé oidc-client hors base) ; NULL = client public (PKCE seul).
    client_secret_enc bytea,
    redirect_path text NOT NULL DEFAULT '/api/v1/auth/oidc/callback'
        CHECK (redirect_path ~ '^/api/v1/auth/oidc/callback'),
    scopes text[] NOT NULL DEFAULT ARRAY['openid', 'profile', 'email', 'groups']::text[],
    email_claim text NOT NULL DEFAULT 'email'
        CHECK (char_length(email_claim) BETWEEN 1 AND 80),
    groups_claim text NOT NULL DEFAULT 'groups'
        CHECK (char_length(groups_claim) BETWEEN 1 AND 80),
    name_claim text NOT NULL DEFAULT 'name'
        CHECK (char_length(name_claim) BETWEEN 1 AND 80),
    step_up_acr text NOT NULL DEFAULT 'urn:rackloom:step-up'
        CHECK (char_length(step_up_acr) BETWEEN 1 AND 120),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid REFERENCES users(id)
);

INSERT INTO oidc_settings (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE oidc_group_mappings (
    id uuid PRIMARY KEY,
    idp_group text NOT NULL CHECK (char_length(btrim(idp_group)) BETWEEN 1 AND 200),
    role text NOT NULL CHECK (role IN (
        'platform_admin', 'template_publisher', 'template_author',
        'operator', 'approver', 'auditor',
        'project_admin', 'connector_admin'
    )),
    project_id uuid REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (idp_group, role, project_id, environment_id),
    CHECK (
        (project_id IS NULL AND role IN (
            'platform_admin', 'template_publisher', 'template_author', 'auditor'
        ))
        OR (project_id IS NOT NULL AND role IN (
            'operator', 'approver', 'auditor', 'project_admin', 'connector_admin',
            'template_author', 'template_publisher'
        ))
    )
);
CREATE INDEX oidc_group_mappings_group ON oidc_group_mappings (idp_group);

-- Provenance des bindings : manuel (UI/seed) vs synchronisé depuis les groupes IdP.
ALTER TABLE role_bindings
    ADD COLUMN IF NOT EXISTS binding_source text NOT NULL DEFAULT 'manual'
        CHECK (binding_source IN ('manual', 'oidc'));
CREATE INDEX role_bindings_oidc ON role_bindings (user_id)
    WHERE revoked_at IS NULL AND binding_source = 'oidc';

-- États OIDC éphémères (PKCE + nonce) ; purgés à l'expiration.
CREATE TABLE oidc_flow_states (
    state text PRIMARY KEY CHECK (char_length(state) BETWEEN 16 AND 128),
    code_verifier text NOT NULL CHECK (char_length(code_verifier) BETWEEN 43 AND 128),
    nonce text NOT NULL CHECK (char_length(nonce) BETWEEN 16 AND 128),
    purpose text NOT NULL CHECK (purpose IN ('login', 'reauth')),
    session_id uuid REFERENCES sessions(id) ON DELETE CASCADE,
    return_to text CHECK (return_to IS NULL OR (return_to ~ '^/' AND return_to !~ '^//')),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > created_at),
    CHECK ((purpose = 'reauth' AND session_id IS NOT NULL) OR (purpose = 'login'))
);
CREATE INDEX oidc_flow_states_expiry ON oidc_flow_states (expires_at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON oidc_settings TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON oidc_group_mappings TO rackloom_api;
        GRANT SELECT, INSERT, DELETE ON oidc_flow_states TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON oidc_settings TO rackloom_worker;
        GRANT SELECT ON oidc_group_mappings TO rackloom_worker;
    END IF;
END $$;
