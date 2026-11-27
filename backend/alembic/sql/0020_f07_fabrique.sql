-- F07 : fabrique d'images (pipeline provenance → build → tests → promotion → dépréciation).
-- L'outil de fabrication réel est ouvert (D11) : connecteur fictif local pour les recettes.
-- Une image dont les tests ont échoué ne peut pas être promue vers le catalogue de production.
-- Déprécier ne retire pas l'image tant qu'elle reste nécessaire (références / promotions).

CREATE TABLE image_builds (
    id uuid PRIMARY KEY,
    slug text NOT NULL CHECK (char_length(slug) BETWEEN 1 AND 80),
    version text NOT NULL CHECK (version ~ '^\d+\.\d+\.\d+$'),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    -- Cycle : draft → built → ready|failed → deprecated (jamais de retrait silencieux).
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'built', 'ready', 'failed', 'deprecated')),
    -- Provenance déclarée à la création (empreintes labo, jamais d'hôte réel).
    source_kind text NOT NULL CHECK (source_kind IN ('iso_digest', 'git_commit', 'artifact_digest')),
    source_digest text NOT NULL CHECK (
        (source_kind = 'git_commit' AND source_digest ~ '^[0-9a-f]{40}$')
        OR (source_kind IN ('iso_digest', 'artifact_digest') AND source_digest ~ '^[0-9a-f]{64}$')
    ),
    recipe_digest text NOT NULL CHECK (recipe_digest ~ '^[0-9a-f]{64}$'),
    builder text NOT NULL DEFAULT 'fake'
        CHECK (builder IN ('fake')),
    -- Attributs inventoriés après construction (filtre clone_image des templates).
    os text NOT NULL CHECK (char_length(os) BETWEEN 1 AND 80),
    layout_id text NOT NULL CHECK (char_length(layout_id) BETWEEN 1 AND 80),
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Résultat du build (absent tant que draft).
    image_digest text CHECK (image_digest IS NULL OR image_digest ~ '^[0-9a-f]{64}$'),
    stable_id text CHECK (stable_id IS NULL OR char_length(stable_id) BETWEEN 1 AND 80),
    simulated boolean NOT NULL DEFAULT true,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    built_at timestamptz,
    tested_at timestamptz,
    deprecated_at timestamptz,
    deprecated_by uuid REFERENCES users(id),
    deprecation_reason text CHECK (deprecation_reason IS NULL OR char_length(deprecation_reason) <= 500),
    UNIQUE (slug, version)
);
CREATE INDEX image_builds_status ON image_builds (status, slug);
CREATE INDEX image_builds_filter ON image_builds (os, layout_id, status);

CREATE TABLE image_build_tests (
    id uuid PRIMARY KEY,
    image_build_id uuid NOT NULL REFERENCES image_builds(id),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 120),
    outcome text NOT NULL CHECK (outcome IN ('passed', 'failed')),
    detail text NOT NULL DEFAULT '' CHECK (char_length(detail) <= 500),
    simulated boolean NOT NULL DEFAULT true,
    ran_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (image_build_id, name)
);
CREATE INDEX image_build_tests_build ON image_build_tests (image_build_id);

CREATE TABLE image_build_promotions (
    id uuid PRIMARY KEY,
    image_build_id uuid NOT NULL REFERENCES image_builds(id),
    environment_kind text NOT NULL CHECK (environment_kind IN ('dev', 'test', 'preprod', 'prod')),
    promoted_by uuid REFERENCES users(id),
    promoted_at timestamptz NOT NULL DEFAULT now(),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    UNIQUE (image_build_id, environment_kind)
);
CREATE INDEX image_build_promotions_kind ON image_build_promotions (environment_kind, promoted_at DESC);

DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        FOREACH t IN ARRAY ARRAY['image_builds', 'image_build_tests', 'image_build_promotions'] LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_api', t);
        END LOOP;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        FOREACH t IN ARRAY ARRAY['image_builds', 'image_build_tests', 'image_build_promotions'] LOOP
            EXECUTE format('GRANT SELECT ON %I TO rackloom_worker', t);
        END LOOP;
    END IF;
END;
$$;
