-- F06 : baux pour environnements temporaires.
-- Durée de vie, propriétaire, rappels, renouvellement ; à l'expiration, proposition
-- d'un plan de retrait. Aucune suppression sans workflow et approbation.
-- Destruction réelle de VM : lot 2 / laboratoire (exécution locale = simulée).

ALTER TABLE changes DROP CONSTRAINT IF EXISTS changes_kind_check;
ALTER TABLE changes ADD CONSTRAINT changes_kind_check
    CHECK (kind IN ('create', 'modify', 'restore', 'restart', 'retire'));

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('inventory_refresh', 'preflight', 'tf_plan', 'tf_apply', 'tf_state_read',
                    'guest_ready', 'ansible_configure', 'verify', 'reconcile', 'drift_scan',
                    'vm_restore', 'service_restart', 'lease_retire'));

CREATE TABLE deployment_leases (
    id uuid PRIMARY KEY,
    deployment_id uuid NOT NULL REFERENCES deployments(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid NOT NULL REFERENCES environments(id),
    owner_user_id uuid NOT NULL REFERENCES users(id),
    expires_on date NOT NULL,
    reminder_on date NOT NULL,
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'reminded', 'expired', 'retirement_proposed', 'closed')),
    reminded_at timestamptz,
    expired_at timestamptz,
    retirement_change_id uuid REFERENCES changes(id),
    closed_at timestamptz,
    close_reason text CHECK (close_reason IS NULL OR length(btrim(close_reason)) BETWEEN 3 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (deployment_id),
    CHECK (reminder_on <= expires_on),
    CHECK ((closed_at IS NULL) = (close_reason IS NULL)),
    CHECK (state <> 'closed' OR closed_at IS NOT NULL)
);
CREATE INDEX deployment_leases_expiry ON deployment_leases (expires_on)
    WHERE state IN ('active', 'reminded', 'expired', 'retirement_proposed');
CREATE INDEX deployment_leases_owner ON deployment_leases (owner_user_id, state);
CREATE INDEX deployment_leases_project ON deployment_leases (project_id, state);

CREATE TABLE deployment_lease_renewals (
    id uuid PRIMARY KEY,
    lease_id uuid NOT NULL REFERENCES deployment_leases(id),
    previous_expires_on date NOT NULL,
    new_expires_on date NOT NULL,
    renewed_by uuid NOT NULL REFERENCES users(id),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (new_expires_on > previous_expires_on)
);
CREATE INDEX deployment_lease_renewals_lease ON deployment_lease_renewals (lease_id, created_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON deployment_leases TO rackloom_api;
        GRANT SELECT, INSERT ON deployment_lease_renewals TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT, INSERT, UPDATE ON deployment_leases TO rackloom_worker;
        GRANT SELECT, INSERT ON deployment_lease_renewals TO rackloom_worker;
    END IF;
END $$;
