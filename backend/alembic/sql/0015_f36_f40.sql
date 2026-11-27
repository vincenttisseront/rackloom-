-- F36 : quotas de concurrence par projet (file équitable).
-- F38 : matrice de compatibilité versionnée.
-- F39 : preuves de sauvegarde (source fictive locale).
-- F40 : changement de restauration + job vm_restore ; réseau de quarantaine.

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS wait_reason text;

ALTER TABLE changes DROP CONSTRAINT IF EXISTS changes_kind_check;
ALTER TABLE changes ADD CONSTRAINT changes_kind_check
    CHECK (kind IN ('create', 'modify', 'restore'));

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('inventory_refresh', 'preflight', 'tf_plan', 'tf_apply', 'tf_state_read',
                    'guest_ready', 'ansible_configure', 'verify', 'reconcile', 'drift_scan',
                    'vm_restore'));

CREATE TABLE execution_quotas (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    job_class text NOT NULL CHECK (job_class IN ('read', 'write', 'all')),
    max_concurrent integer NOT NULL CHECK (max_concurrent > 0),
    -- Priorité d'ordonnancement des jobs du projet (plus petit = plus prioritaire).
    priority integer NOT NULL DEFAULT 100 CHECK (priority > 0 AND priority < 10000),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, environment_id, job_class)
);
CREATE INDEX execution_quotas_project ON execution_quotas (project_id);

CREATE TABLE compatibility_matrix (
    id uuid PRIMARY KEY,
    matrix_version text NOT NULL CHECK (char_length(matrix_version) <= 40),
    component_kind text NOT NULL CHECK (component_kind IN (
        'terraform_provider', 'terraform', 'ansible_role', 'component', 'runner_image'
    )),
    name text NOT NULL CHECK (char_length(name) <= 200),
    version text NOT NULL CHECK (char_length(version) <= 80),
    status text NOT NULL CHECK (status IN ('verified', 'unverified', 'incompatible')),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (matrix_version, component_kind, name, version)
);
CREATE INDEX compatibility_matrix_lookup
    ON compatibility_matrix (matrix_version, component_kind, name, version);

CREATE TABLE backup_proofs (
    id uuid PRIMARY KEY,
    -- Identité VM déclarée (nom ou UUID) — source fictive locale, jamais un secret.
    vm_identity text NOT NULL CHECK (char_length(vm_identity) <= 200),
    status text NOT NULL CHECK (status IN ('ok', 'stale', 'missing', 'unreachable')),
    -- approved_backup = preuve acceptée ; vsphere_snapshot = refusé (ne vaut pas sauvegarde).
    proof_kind text NOT NULL CHECK (proof_kind IN ('approved_backup', 'vsphere_snapshot')),
    last_backup_at timestamptz,
    reachable boolean NOT NULL DEFAULT true,
    simulated boolean NOT NULL DEFAULT true,
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (vm_identity)
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT ON execution_quotas TO rackloom_api;
        GRANT SELECT ON compatibility_matrix TO rackloom_api;
        GRANT SELECT ON backup_proofs TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON execution_quotas TO rackloom_worker;
        GRANT SELECT ON compatibility_matrix TO rackloom_worker;
        GRANT SELECT ON backup_proofs TO rackloom_worker;
    END IF;
END $$;
