-- F18 : sondes déclaratives et objectifs de disponibilité.
-- Distingue état du service (OS), santé de la sonde et fraîcheur.
-- Un service actif reste « unavailable » si la sonde échoue depuis le segment prévu.

ALTER TABLE deployments DROP CONSTRAINT IF EXISTS deployments_service_status_check;
ALTER TABLE deployments ADD CONSTRAINT deployments_service_status_check
    CHECK (service_status IN (
        'unknown', 'ready', 'created_not_ready', 'failed',
        'verification_unavailable', 'unavailable'
    ));

CREATE TABLE probe_segments (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE
        CHECK (slug ~ '^[a-z][a-z0-9-]{0,62}$'),
    name text NOT NULL
        CHECK (char_length(name) BETWEEN 1 AND 120),
    -- Zone F16 d'où partent les runners de sonde (NULL = zone locale implicite).
    zone_slug text REFERENCES execution_zones(slug),
    network_name text NOT NULL
        CHECK (char_length(network_name) BETWEEN 1 AND 120),
    allowed_protocols text[] NOT NULL DEFAULT ARRAY['http']::text[]
        CHECK (allowed_protocols <@ ARRAY['http', 'https', 'tcp']::text[]
               AND cardinality(allowed_protocols) >= 1),
    -- false = segment filtrant (n'atteint pas les cibles) : sonde échoue, service peut rester actif.
    reaches_targets boolean NOT NULL DEFAULT true,
    is_builtin boolean NOT NULL DEFAULT false,
    simulated boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO probe_segments (id, slug, name, zone_slug, network_name, allowed_protocols, reaches_targets, is_builtin, simulated)
VALUES
    ('01940000-f18a-7000-8000-000000000001', 'lab-access', 'Segment d''accès labo',
     'dev', 'rn-lab', ARRAY['http', 'https', 'tcp']::text[], true, true, true),
    ('01940000-f18a-7000-8000-000000000002', 'filtered', 'Segment filtrant (isolation)',
     'dev', 'rn-filtered', ARRAY['http', 'https', 'tcp']::text[], false, true, true);

CREATE TABLE availability_objectives (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    deployment_id uuid REFERENCES deployments(id),
    name text NOT NULL
        CHECK (char_length(name) BETWEEN 1 AND 120),
    -- Objectif de disponibilité (pourcentage). Aucune facture ni SLA inventé : cible déclarée.
    target_pct numeric(5, 2) NOT NULL
        CHECK (target_pct > 0 AND target_pct <= 100),
    max_probe_age_seconds integer NOT NULL DEFAULT 900
        CHECK (max_probe_age_seconds BETWEEN 60 AND 86400),
    probe_segment_slug text NOT NULL REFERENCES probe_segments(slug),
    protocols text[] NOT NULL DEFAULT ARRAY['http']::text[]
        CHECK (protocols <@ ARRAY['http', 'https', 'tcp']::text[]
               AND cardinality(protocols) >= 1),
    active boolean NOT NULL DEFAULT true,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX availability_objectives_project ON availability_objectives (project_id, active);
CREATE INDEX availability_objectives_deployment ON availability_objectives (deployment_id)
    WHERE deployment_id IS NOT NULL;

CREATE TABLE probe_results (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    deployment_id uuid NOT NULL REFERENCES deployments(id),
    host_id uuid NOT NULL REFERENCES hosts(id),
    objective_id uuid REFERENCES availability_objectives(id),
    protocol text NOT NULL CHECK (protocol IN ('http', 'https', 'tcp')),
    from_segment text NOT NULL REFERENCES probe_segments(slug),
    -- Dimension 1 : état OS / processus (indépendant de la sonde réseau).
    service_state text NOT NULL CHECK (service_state IN ('active', 'inactive', 'unknown')),
    -- Dimension 2 : résultat de la sonde depuis le segment prévu.
    probe_health text NOT NULL CHECK (probe_health IN ('ok', 'failed', 'error', 'skipped')),
    -- Dimension 3 (fraîcheur) : dérivée à la lecture via checked_at + max_probe_age.
    checked_at timestamptz NOT NULL DEFAULT now(),
    -- Synthèse : available seulement si service actif ET sonde ok (fraîcheur à part).
    availability text NOT NULL CHECK (availability IN ('available', 'unavailable', 'unknown')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    job_id uuid REFERENCES jobs(id),
    simulated_filter boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX probe_results_deployment ON probe_results (deployment_id, checked_at DESC);
CREATE INDEX probe_results_host ON probe_results (host_id, checked_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON probe_segments TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON availability_objectives TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON probe_results TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON probe_segments TO rackloom_worker;
        GRANT SELECT ON availability_objectives TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE ON probe_results TO rackloom_worker;
    END IF;
END $$;
