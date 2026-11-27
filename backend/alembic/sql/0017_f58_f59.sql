-- F58 : actions courantes depuis la fiche service (change kind restart + job).
-- F59 : demande d'accès — correspondances approuvées, demandes, faux IdP local.

ALTER TABLE changes DROP CONSTRAINT IF EXISTS changes_kind_check;
ALTER TABLE changes ADD CONSTRAINT changes_kind_check
    CHECK (kind IN ('create', 'modify', 'restore', 'restart'));

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('inventory_refresh', 'preflight', 'tf_plan', 'tf_apply', 'tf_state_read',
                    'guest_ready', 'ansible_configure', 'verify', 'reconcile', 'drift_scan',
                    'vm_restore', 'service_restart'));

-- Correspondances niveau d'accès → groupe IdP (versionnées, jamais saisies par le demandeur).
CREATE TABLE service_access_mappings (
    id uuid PRIMARY KEY,
    template_slug text NOT NULL CHECK (char_length(template_slug) BETWEEN 1 AND 80),
    environment_kind text NOT NULL CHECK (environment_kind IN ('dev', 'test', 'preprod', 'prod')),
    access_level text NOT NULL CHECK (char_length(access_level) BETWEEN 1 AND 40),
    access_label text NOT NULL CHECK (char_length(access_label) BETWEEN 1 AND 120),
    idp_group text NOT NULL CHECK (char_length(idp_group) BETWEEN 1 AND 200),
    version integer NOT NULL CHECK (version >= 1),
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'superseded')),
    published_at timestamptz,
    published_by uuid REFERENCES users(id),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (template_slug, environment_kind, access_level, version)
);
CREATE UNIQUE INDEX service_access_mappings_published_uq
    ON service_access_mappings (template_slug, environment_kind, access_level)
    WHERE status = 'published';
CREATE INDEX service_access_mappings_slug ON service_access_mappings (template_slug, status);

-- Demandes d'accès à un service livré.
CREATE TABLE service_access_requests (
    id uuid PRIMARY KEY,
    deployment_id uuid NOT NULL REFERENCES deployments(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid NOT NULL REFERENCES environments(id),
    requester_id uuid NOT NULL REFERENCES users(id),
    subject_user_id uuid NOT NULL REFERENCES users(id),
    access_level text NOT NULL CHECK (char_length(access_level) BETWEEN 1 AND 40),
    mapping_id uuid NOT NULL REFERENCES service_access_mappings(id),
    idp_group text NOT NULL CHECK (char_length(idp_group) BETWEEN 1 AND 200),
    state text NOT NULL DEFAULT 'pending_approval'
        CHECK (state IN (
            'pending_approval', 'rejected', 'cancelled',
            'approved', 'syncing', 'granted', 'sync_failed'
        )),
    -- Clé d'idempotence envoyée à l'IdP ; une relance la réutilise.
    idempotency_key text NOT NULL UNIQUE
        CHECK (char_length(idempotency_key) BETWEEN 8 AND 200),
    idp_assignment_id text CHECK (idp_assignment_id IS NULL OR char_length(idp_assignment_id) BETWEEN 1 AND 200),
    sync_error text CHECK (sync_error IS NULL OR char_length(sync_error) <= 500),
    sync_attempts integer NOT NULL DEFAULT 0 CHECK (sync_attempts >= 0),
    approved_by uuid REFERENCES users(id),
    approved_at timestamptz,
    rejected_by uuid REFERENCES users(id),
    rejected_at timestamptz,
    rejection_comment text CHECK (rejection_comment IS NULL OR char_length(rejection_comment) <= 500),
    granted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX service_access_requests_deployment ON service_access_requests (deployment_id, created_at DESC);
CREATE INDEX service_access_requests_project ON service_access_requests (project_id, state);
CREATE INDEX service_access_requests_subject ON service_access_requests (subject_user_id, state);

-- Attributions confirmées par l'IdP (anti-doublon hors clé d'idempotence).
CREATE TABLE service_access_assignments (
    id uuid PRIMARY KEY,
    request_id uuid NOT NULL REFERENCES service_access_requests(id),
    deployment_id uuid NOT NULL REFERENCES deployments(id),
    subject_user_id uuid NOT NULL REFERENCES users(id),
    idp_group text NOT NULL,
    idp_assignment_id text NOT NULL UNIQUE,
    idempotency_key text NOT NULL UNIQUE,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    simulated boolean NOT NULL DEFAULT true
);
CREATE INDEX service_access_assignments_dep ON service_access_assignments (deployment_id);

-- État du faux IdP local (échecs injectés pour les recettes).
CREATE TABLE idp_fake_state (
    id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    fail_next boolean NOT NULL DEFAULT false,
    fail_message text NOT NULL DEFAULT 'échec de synchronisation IdP (simulé)'
        CHECK (char_length(fail_message) <= 500),
    assignment_count integer NOT NULL DEFAULT 0 CHECK (assignment_count >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO idp_fake_state (id) VALUES (1) ON CONFLICT DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON service_access_mappings TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON service_access_requests TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON service_access_assignments TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON idp_fake_state TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON service_access_mappings TO rackloom_worker;
        GRANT SELECT, UPDATE ON service_access_requests TO rackloom_worker;
        GRANT SELECT, INSERT ON service_access_assignments TO rackloom_worker;
        GRANT SELECT, UPDATE ON idp_fake_state TO rackloom_worker;
    END IF;
END $$;
