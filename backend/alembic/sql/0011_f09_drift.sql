-- F09 : détection de dérive (lecture seule, aucune correction automatique).

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('inventory_refresh', 'preflight', 'tf_plan', 'tf_apply', 'tf_state_read',
                    'guest_ready', 'ansible_configure', 'verify', 'reconcile', 'drift_scan'));

CREATE TABLE drift_scans (
    id uuid PRIMARY KEY,
    project_id uuid REFERENCES projects(id),
    deployment_id uuid REFERENCES deployments(id),
    status text NOT NULL CHECK (status IN ('ok', 'drift', 'observation_unavailable')),
    source text NOT NULL DEFAULT 'system',
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    job_id uuid REFERENCES jobs(id)
);
CREATE INDEX drift_scans_deployment ON drift_scans (deployment_id, started_at DESC);

CREATE TABLE drift_findings (
    id uuid PRIMARY KEY,
    scan_id uuid NOT NULL REFERENCES drift_scans(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    deployment_id uuid REFERENCES deployments(id),
    resource_name text NOT NULL,
    attribute text NOT NULL,
    expected text,
    observed text,
    source text NOT NULL,
    detected_at timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL CHECK (kind IN ('drift', 'observation_unavailable'))
);
CREATE INDEX drift_findings_deployment ON drift_findings (deployment_id, detected_at DESC);

CREATE TRIGGER drift_scans_append_only
    BEFORE UPDATE OR DELETE ON drift_scans
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();
CREATE TRIGGER drift_findings_append_only
    BEFORE UPDATE OR DELETE ON drift_findings
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        FOREACH t IN ARRAY ARRAY['drift_scans', 'drift_findings'] LOOP
            EXECUTE format('GRANT SELECT, INSERT ON %I TO rackloom_api', t);
        END LOOP;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        FOREACH t IN ARRAY ARRAY['drift_scans', 'drift_findings'] LOOP
            EXECUTE format('GRANT SELECT, INSERT ON %I TO rackloom_worker', t);
        END LOOP;
    END IF;
END;
$$;
