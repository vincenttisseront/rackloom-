-- F16 : zones d'exécution et runners de site enrôlés par mTLS.
-- Le runner initie la connexion ; le serveur filtre les jobs par zone et capacités.
-- Une perte de heartbeat marque les écritures envoyées comme incertaines (réconciliation,
-- jamais une seconde écriture non vérifiée).

CREATE TABLE execution_zones (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    -- kinds d'environnement autorisés pour cette zone (ex. {dev,test} ou {prod}).
    environment_kinds text[] NOT NULL
        CHECK (cardinality(environment_kinds) >= 1
               AND environment_kinds <@ ARRAY['dev', 'test', 'preprod', 'prod']::text[]),
    is_builtin boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE site_runners (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    zone_id uuid NOT NULL REFERENCES execution_zones(id),
    -- Capacités déclarées (kinds de jobs que ce runner peut prendre).
    capabilities text[] NOT NULL
        CHECK (cardinality(capabilities) >= 1),
    -- Identité mTLS : empreinte SHA-256 du certificat client (hex minuscule, 64).
    cert_fingerprint_sha256 text NOT NULL UNIQUE
        CHECK (cert_fingerprint_sha256 ~ '^[a-f0-9]{64}$'),
    cert_subject text NOT NULL CHECK (char_length(cert_subject) BETWEEN 1 AND 500),
    cert_not_before timestamptz NOT NULL,
    cert_not_after timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'enrolled'
        CHECK (status IN ('enrolled', 'active', 'stale', 'revoked')),
    last_heartbeat_at timestamptz,
    enrolled_at timestamptz NOT NULL DEFAULT now(),
    enrolled_by uuid REFERENCES users(id),
    revoked_at timestamptz,
    revoked_by uuid REFERENCES users(id),
    revoke_reason text CHECK (revoke_reason IS NULL OR char_length(revoke_reason) BETWEEN 1 AND 500),
    -- Identifiant de worker local lié après le premier heartbeat.
    worker_id text,
    CHECK (cert_not_after > cert_not_before),
    CHECK (status <> 'revoked' OR revoked_at IS NOT NULL)
);

CREATE INDEX site_runners_zone ON site_runners (zone_id) WHERE status <> 'revoked';
CREATE INDEX site_runners_heartbeat ON site_runners (last_heartbeat_at)
    WHERE status IN ('enrolled', 'active');
CREATE INDEX site_runners_worker ON site_runners (worker_id) WHERE worker_id IS NOT NULL;

-- Zone requise pour le job (résolue à la mise en file depuis l'environnement).
-- NULL : job système sans environnement (réconciliation, inventaire) — tout runner actif
-- ayant la capacité peut le prendre, sous réserve des autres filtres.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS required_zone text
        CHECK (required_zone IS NULL OR required_zone ~ '^[a-z][a-z0-9-]{1,40}$');

CREATE INDEX jobs_required_zone ON jobs (required_zone) WHERE state = 'queued';

-- Zones de démonstration : dev ≠ prod (un runner de dev ne prend jamais un job de prod).
INSERT INTO execution_zones (id, slug, name, environment_kinds, is_builtin)
VALUES
    ('01940000-f16a-7000-8000-000000000001', 'dev', 'Zone de développement',
     ARRAY['dev', 'test']::text[], true),
    ('01940000-f16a-7000-8000-000000000002', 'prod', 'Zone de production',
     ARRAY['preprod', 'prod']::text[], true)
ON CONFLICT (slug) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON execution_zones TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON site_runners TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON execution_zones TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE ON site_runners TO rackloom_worker;
    END IF;
END $$;
