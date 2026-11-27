-- F24 : conformité des machines (profils versionnés, scans lecture seule).
-- Remédiation = brouillon de demande uniquement ; aucune écriture fournisseur ici.
-- Connecteur Rudder optionnel : observations fictives explicites (RAC-36 / D2).

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('inventory_refresh', 'preflight', 'tf_plan', 'tf_apply', 'tf_state_read',
                    'guest_ready', 'ansible_configure', 'verify', 'reconcile', 'drift_scan',
                    'vm_restore', 'service_restart', 'lease_retire', 'tf_import',
                    'host_patch', 'patch_check', 'compliance_scan'));

ALTER TABLE host_observations DROP CONSTRAINT IF EXISTS host_observations_kind_check;
ALTER TABLE host_observations ADD CONSTRAINT host_observations_kind_check
    CHECK (kind IN ('facts', 'packages', 'services', 'probe', 'journal', 'ssh', 'settings'));

-- Profils de conformité versionnés (immuables une fois publiés).
CREATE TABLE compliance_profiles (
    id uuid PRIMARY KEY,
    slug text NOT NULL CHECK (char_length(slug) BETWEEN 1 AND 80),
    version text NOT NULL CHECK (char_length(version) BETWEEN 1 AND 40),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 160),
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'published', 'superseded')),
    -- Règles : packages[], services[], settings[] (sélections seulement).
    rules jsonb NOT NULL DEFAULT '{}'::jsonb,
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    published_at timestamptz,
    published_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (slug, version)
);
CREATE UNIQUE INDEX compliance_profiles_published_uq
    ON compliance_profiles (slug) WHERE status = 'published';
CREATE INDEX compliance_profiles_status ON compliance_profiles (status, slug);

-- Scans append-only (aucune correction automatique).
CREATE TABLE compliance_scans (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    host_id uuid NOT NULL REFERENCES hosts(id),
    profile_id uuid NOT NULL REFERENCES compliance_profiles(id),
    status text NOT NULL CHECK (status IN (
        'ok', 'noncompliant', 'partial', 'no_data'
    )),
    source text NOT NULL DEFAULT 'rackloom'
        CHECK (source IN ('rackloom', 'rudder_fake', 'system')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    job_id uuid REFERENCES jobs(id)
);
CREATE INDEX compliance_scans_host ON compliance_scans (host_id, started_at DESC);
CREATE INDEX compliance_scans_project ON compliance_scans (project_id, started_at DESC);

CREATE TABLE compliance_findings (
    id uuid PRIMARY KEY,
    scan_id uuid NOT NULL REFERENCES compliance_scans(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    host_id uuid NOT NULL REFERENCES hosts(id),
    rule_id text NOT NULL CHECK (char_length(rule_id) BETWEEN 1 AND 120),
    category text NOT NULL CHECK (category IN ('package', 'service', 'setting')),
    attribute text NOT NULL CHECK (char_length(attribute) BETWEEN 1 AND 160),
    expected text,
    observed text,
    -- Trois résultats distincts exigés par F24 (hors conforme).
    status text NOT NULL CHECK (status IN (
        'compliant', 'noncompliant', 'not_applicable', 'missing_data'
    )),
    detected_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX compliance_findings_scan ON compliance_findings (scan_id);
CREATE INDEX compliance_findings_host ON compliance_findings (host_id, detected_at DESC);

CREATE TRIGGER compliance_scans_append_only
    BEFORE UPDATE OR DELETE ON compliance_scans
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();
CREATE TRIGGER compliance_findings_append_only
    BEFORE UPDATE OR DELETE ON compliance_findings
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

-- Profil de démonstration ubuntu-web (paquets / services / réglage nginx).
INSERT INTO compliance_profiles (
    id, slug, version, title, status, rules, note, published_at
) VALUES (
    '01940000-f024-7000-8000-000000000001'::uuid,
    'ubuntu-web-baseline',
    '1.0.0',
    'Socle serveur web Ubuntu',
    'published',
    '{
      "packages": [
        {"id": "pkg.nginx", "name": "nginx", "state": "installed"}
      ],
      "services": [
        {"id": "svc.nginx", "unit": "nginx", "state": "active"}
      ],
      "settings": [
        {
          "id": "set.nginx.server_tokens",
          "key": "nginx.server_tokens",
          "expected": "off",
          "when": {"package": "nginx"}
        },
        {
          "id": "set.fail2ban.enabled",
          "key": "fail2ban.enabled",
          "expected": "true",
          "when": {"package": "fail2ban"}
        }
      ]
    }'::jsonb,
    'Profil de démonstration F24 (immuable une fois publié).',
    now()
);

DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        FOREACH t IN ARRAY ARRAY[
            'compliance_profiles', 'compliance_scans', 'compliance_findings'
        ] LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_api', t);
        END LOOP;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        FOREACH t IN ARRAY ARRAY[
            'compliance_profiles', 'compliance_scans', 'compliance_findings'
        ] LOOP
            EXECUTE format('GRANT SELECT, INSERT ON %I TO rackloom_worker', t);
        END LOOP;
    END IF;
END;
$$;
