-- F08 : adoption de VM existantes.
-- Enrôlement et observation en lecture seule d'abord ; proposition d'import Terraform
-- séparée de son exécution ; conflit de propriété (autre state) bloque l'import.
-- Observations fictives explicites (pas de labo vSphere réel — D2 / D21).

ALTER TABLE resource_grants DROP CONSTRAINT IF EXISTS resource_grants_kind_check;
ALTER TABLE resource_grants ADD CONSTRAINT resource_grants_kind_check
    CHECK (kind IN (
        'cluster', 'datastore', 'folder', 'network', 'image',
        'service_template', 'vm'
    ));

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('inventory_refresh', 'preflight', 'tf_plan', 'tf_apply', 'tf_state_read',
                    'guest_ready', 'ansible_configure', 'verify', 'reconcile', 'drift_scan',
                    'vm_restore', 'service_restart', 'lease_retire', 'tf_import'));

-- Revendications de propriété Terraform (Rackloom ou externe) sur une VM.
-- Une seule revendication active par vm_stable_id : sert à détecter la gestion concurrente.
CREATE TABLE terraform_resource_claims (
    id uuid PRIMARY KEY,
    vm_stable_id text NOT NULL,
    claim_source text NOT NULL CHECK (claim_source IN ('rackloom', 'external')),
    state_id text NOT NULL CHECK (char_length(state_id) BETWEEN 1 AND 200),
    project_id uuid REFERENCES projects(id),
    adoption_id uuid,
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    simulated boolean NOT NULL DEFAULT true,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (vm_stable_id)
);
CREATE INDEX terraform_resource_claims_project ON terraform_resource_claims (project_id);

CREATE TABLE vm_adoptions (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid NOT NULL REFERENCES environments(id),
    vm_stable_id text NOT NULL,
    vm_name text NOT NULL,
    connector_slug text NOT NULL DEFAULT 'vsphere-fake'
        CHECK (char_length(connector_slug) BETWEEN 1 AND 80),
    state text NOT NULL DEFAULT 'observing'
        CHECK (state IN (
            'observing',
            'preview_ready',
            'import_proposed',
            'blocked_conflict',
            'imported',
            'cancelled'
        )),
    ownership text NOT NULL DEFAULT 'unmanaged'
        CHECK (ownership IN ('unmanaged', 'rackloom', 'external_state', 'conflict')),
    conflict_reason text CHECK (conflict_reason IS NULL OR char_length(conflict_reason) <= 500),
    external_state_id text CHECK (external_state_id IS NULL OR char_length(external_state_id) BETWEEN 1 AND 200),
    -- Dernière observation (fictive) ; ne constitue jamais une autorisation d'écriture.
    observation jsonb NOT NULL DEFAULT '{}'::jsonb,
    observation_fictional boolean NOT NULL DEFAULT true,
    -- Aperçu d'import (lecture seule) : ne modifie jamais la VM ni le state.
    preview jsonb,
    previewed_at timestamptz,
    -- Proposition d'import Terraform (séparée de l'exécution).
    import_proposal jsonb,
    import_proposal_sha256 text CHECK (
        import_proposal_sha256 IS NULL OR char_length(import_proposal_sha256) = 64
    ),
    proposed_at timestamptz,
    proposed_by uuid REFERENCES users(id),
    -- Approbation liée à l'empreinte de la proposition (une seule utilisation).
    approved_at timestamptz,
    approved_by uuid REFERENCES users(id),
    approval_proposal_sha256 text CHECK (
        approval_proposal_sha256 IS NULL OR char_length(approval_proposal_sha256) = 64
    ),
    imported_at timestamptz,
    import_job_id uuid REFERENCES jobs(id),
    enrolled_by uuid NOT NULL REFERENCES users(id),
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, vm_stable_id)
);
CREATE INDEX vm_adoptions_project ON vm_adoptions (project_id, state);
CREATE INDEX vm_adoptions_vm ON vm_adoptions (vm_stable_id);

ALTER TABLE terraform_resource_claims
    ADD CONSTRAINT terraform_resource_claims_adoption_fk
    FOREIGN KEY (adoption_id) REFERENCES vm_adoptions(id);

-- Historique d'observations d'adoption (append-only, fictives).
CREATE TABLE adoption_observations (
    id bigserial PRIMARY KEY,
    adoption_id uuid NOT NULL REFERENCES vm_adoptions(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    data jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('ok', 'error', 'unreachable')),
    source text NOT NULL DEFAULT 'fictional'
        CHECK (char_length(source) BETWEEN 1 AND 80),
    fictional boolean NOT NULL DEFAULT true,
    collected_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX adoption_observations_adoption ON adoption_observations (adoption_id, collected_at DESC);

CREATE TRIGGER adoption_observations_append_only
    BEFORE UPDATE OR DELETE ON adoption_observations
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON vm_adoptions TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON terraform_resource_claims TO rackloom_api;
        GRANT SELECT, INSERT ON adoption_observations TO rackloom_api;
        GRANT USAGE ON SEQUENCE adoption_observations_id_seq TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT, INSERT, UPDATE ON vm_adoptions TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE ON terraform_resource_claims TO rackloom_worker;
        GRANT SELECT, INSERT ON adoption_observations TO rackloom_worker;
        GRANT USAGE ON SEQUENCE adoption_observations_id_seq TO rackloom_worker;
    END IF;
END;
$$;
