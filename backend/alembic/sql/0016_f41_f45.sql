-- F41 : SCIM (external_id, jetons de provisioning).
-- F42 : campagnes de revue d'accès périodique.
-- F43 : rotation d'identifiants de connecteurs.
-- F44 : copie d'audit hors appliance (livraisons + destination).
-- F45 : registre des certificats TLS des endpoints approuvés.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS external_id text
        CHECK (external_id IS NULL OR char_length(external_id) BETWEEN 1 AND 200);

CREATE UNIQUE INDEX IF NOT EXISTS users_external_id_uq
    ON users (external_id) WHERE external_id IS NOT NULL;

CREATE TABLE scim_tokens (
    id uuid PRIMARY KEY,
    token_hash text NOT NULL UNIQUE,
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 120),
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid REFERENCES users(id),
    last_used_at timestamptz,
    revoked_at timestamptz,
    revoked_by uuid REFERENCES users(id)
);

CREATE TABLE scim_sync_events (
    id uuid PRIMARY KEY,
    external_id text NOT NULL CHECK (char_length(external_id) BETWEEN 1 AND 200),
    user_id uuid REFERENCES users(id),
    operation text NOT NULL CHECK (operation IN ('create', 'update', 'deactivate', 'reactivate')),
    active boolean NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL DEFAULT 'scim',
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX scim_sync_events_user ON scim_sync_events (user_id, at DESC);
CREATE INDEX scim_sync_events_ext ON scim_sync_events (external_id, at DESC);

-- F42 : campagnes de revue d'accès.
CREATE TABLE access_review_campaigns (
    id uuid PRIMARY KEY,
    title text NOT NULL CHECK (char_length(title) BETWEEN 3 AND 200),
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed')),
    prepared_at timestamptz NOT NULL DEFAULT now(),
    prepared_by uuid REFERENCES users(id),
    closed_at timestamptz,
    closed_by uuid REFERENCES users(id),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500)
);

CREATE TABLE access_review_items (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES access_review_campaigns(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    kind text NOT NULL CHECK (kind IN (
        'orphan_account', 'unjustified_binding', 'expired_exception', 'expired_temporary_grant'
    )),
    subject_user_id uuid REFERENCES users(id),
    binding_id uuid,
    exception_id uuid,
    temporary_grant_id uuid,
    summary text NOT NULL CHECK (char_length(summary) BETWEEN 3 AND 500),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'revoked')),
    decided_at timestamptz,
    decided_by uuid REFERENCES users(id),
    decision_note text CHECK (decision_note IS NULL OR char_length(decision_note) BETWEEN 1 AND 500),
    UNIQUE (campaign_id, kind, binding_id),
    UNIQUE (campaign_id, kind, exception_id),
    UNIQUE (campaign_id, kind, temporary_grant_id),
    UNIQUE (campaign_id, kind, subject_user_id, project_id)
);
CREATE INDEX access_review_items_project ON access_review_items (project_id, status);
CREATE INDEX access_review_items_campaign ON access_review_items (campaign_id);

-- F43 : rotation d'identifiants de connecteurs.
CREATE TABLE connector_credential_rotations (
    id uuid PRIMARY KEY,
    connector_id uuid NOT NULL REFERENCES connectors(id),
    secret_name text NOT NULL CHECK (char_length(secret_name) BETWEEN 1 AND 40),
    old_ref text NOT NULL CHECK (old_ref LIKE 'secret://%'),
    candidate_ref text NOT NULL CHECK (candidate_ref LIKE 'secret://%'),
    status text NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'validated', 'cutover', 'failed', 'retired', 'cancelled')),
    test_result jsonb,
    tested_at timestamptz,
    cutover_at timestamptz,
    retired_at timestamptz,
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    failure_reason text CHECK (failure_reason IS NULL OR char_length(failure_reason) <= 500),
    -- Une seule rotation active (non terminée) par connecteur + nom de secret.
    CHECK (status IN ('cutover', 'failed', 'retired', 'cancelled')
           OR status IN ('proposed', 'validated'))
);
CREATE UNIQUE INDEX connector_rotations_active
    ON connector_credential_rotations (connector_id, secret_name)
    WHERE status IN ('proposed', 'validated', 'cutover');
CREATE INDEX connector_rotations_connector ON connector_credential_rotations (connector_id, created_at DESC);

-- F44 : copie d'audit hors appliance.
CREATE TABLE audit_export_destinations (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    kind text NOT NULL DEFAULT 'local_object_lock'
        CHECK (kind IN ('local_object_lock')),
    -- Chemin relatif sous data_dir ; jamais un secret.
    relative_path text NOT NULL CHECK (char_length(relative_path) BETWEEN 1 AND 200),
    object_lock boolean NOT NULL DEFAULT true,
    simulated boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid REFERENCES users(id)
);

CREATE TABLE audit_export_deliveries (
    id uuid PRIMARY KEY,
    destination_id uuid NOT NULL REFERENCES audit_export_destinations(id),
    audit_id bigint NOT NULL REFERENCES audit_log(id),
    -- Empreinte de la ligne d'audit : dedupe sans doublon après reprise.
    dedupe_key text NOT NULL,
    object_key text NOT NULL CHECK (char_length(object_key) BETWEEN 1 AND 300),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text CHECK (last_error IS NULL OR char_length(last_error) <= 500),
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (destination_id, dedupe_key),
    UNIQUE (destination_id, audit_id)
);
CREATE INDEX audit_export_deliveries_pending
    ON audit_export_deliveries (destination_id, status) WHERE status <> 'delivered';

CREATE TABLE audit_export_cursors (
    destination_id uuid PRIMARY KEY REFERENCES audit_export_destinations(id),
    last_audit_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- F45 : registre certificats TLS (pas de clé privée).
CREATE TABLE tls_certificate_records (
    id uuid PRIMARY KEY,
    endpoint_id uuid NOT NULL REFERENCES approved_endpoints(id),
    fingerprint_sha256 text NOT NULL CHECK (char_length(fingerprint_sha256) = 64),
    subject text NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 500),
    issuer text NOT NULL CHECK (char_length(issuer) BETWEEN 1 AND 500),
    not_before timestamptz NOT NULL,
    not_after timestamptz NOT NULL,
    san jsonb NOT NULL DEFAULT '[]'::jsonb,
    chain_depth integer NOT NULL DEFAULT 1 CHECK (chain_depth >= 1),
    owner_user_id uuid REFERENCES users(id),
    alert_days integer NOT NULL DEFAULT 30 CHECK (alert_days BETWEEN 1 AND 365),
    last_scanned_at timestamptz NOT NULL DEFAULT now(),
    alert_status text NOT NULL DEFAULT 'ok'
        CHECK (alert_status IN ('ok', 'expiring_soon', 'expired', 'unreachable', 'untrusted')),
    alert_message text NOT NULL DEFAULT '' CHECK (char_length(alert_message) <= 500),
    simulated boolean NOT NULL DEFAULT false,
    UNIQUE (endpoint_id)
);
CREATE INDEX tls_certificate_records_alert ON tls_certificate_records (alert_status, not_after);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON scim_tokens TO rackloom_api;
        GRANT SELECT, INSERT ON scim_sync_events TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON access_review_campaigns TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON access_review_items TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON connector_credential_rotations TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON audit_export_destinations TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON audit_export_deliveries TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON audit_export_cursors TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON tls_certificate_records TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON scim_tokens TO rackloom_worker;
        GRANT SELECT ON scim_sync_events TO rackloom_worker;
        GRANT SELECT ON access_review_campaigns TO rackloom_worker;
        GRANT SELECT ON access_review_items TO rackloom_worker;
        GRANT SELECT ON connector_credential_rotations TO rackloom_worker;
        GRANT SELECT ON audit_export_destinations TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE ON audit_export_deliveries TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE ON audit_export_cursors TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE ON tls_certificate_records TO rackloom_worker;
    END IF;
END $$;
