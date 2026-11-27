-- F17 : bundles signés pour sites déconnectés.
-- Lots (images, dépendances, providers Terraform, collections Ansible, modèles)
-- signés Ed25519 ; contenu exact affiché ; intégrité et compatibilité avant import.
-- Un octet altéré → refus. Aucun job créé. Clé privée hors base (keys_dir).

CREATE TABLE offline_bundle_trust_keys (
    id uuid PRIMARY KEY,
    key_id text NOT NULL UNIQUE CHECK (key_id ~ '^[0-9a-f]{16}$'),
    public_key bytea NOT NULL CHECK (octet_length(public_key) = 32),
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 120),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE offline_bundles (
    id uuid PRIMARY KEY,
    slug text NOT NULL CHECK (char_length(slug) BETWEEN 1 AND 80),
    version text NOT NULL CHECK (version ~ '^\d+\.\d+\.\d+$'),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    -- Cycle : inspected → imported | rejected (jamais d'import sans vérif).
    status text NOT NULL DEFAULT 'inspected'
        CHECK (status IN ('inspected', 'imported', 'rejected')),
    format text NOT NULL DEFAULT 'rackloom.offline_bundle.v1'
        CHECK (format = 'rackloom.offline_bundle.v1'),
    -- Manifeste exact (contenu affiché) et empreinte signée.
    manifest jsonb NOT NULL,
    content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    signature text NOT NULL CHECK (signature ~ '^[0-9a-f]{128}$'),
    signer_key_id text NOT NULL REFERENCES offline_bundle_trust_keys(key_id),
    -- Compatibilité appliance déclarée dans le manifeste.
    appliance_min_version text NOT NULL,
    appliance_max_version text NOT NULL,
    appliance_arch text NOT NULL DEFAULT 'amd64',
    appliance_os text NOT NULL DEFAULT 'ubuntu-24.04',
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    storage_path text,
    reject_code text,
    reject_reason text CHECK (reject_reason IS NULL OR char_length(reject_reason) <= 500),
    inspected_by uuid REFERENCES users(id),
    inspected_at timestamptz NOT NULL DEFAULT now(),
    imported_by uuid REFERENCES users(id),
    imported_at timestamptz,
    UNIQUE (content_digest)
);
CREATE INDEX offline_bundles_status ON offline_bundles (status, inspected_at DESC);
CREATE INDEX offline_bundles_slug ON offline_bundles (slug, version);

CREATE TABLE offline_bundle_items (
    id uuid PRIMARY KEY,
    bundle_id uuid NOT NULL REFERENCES offline_bundles(id),
    kind text NOT NULL CHECK (kind IN (
        'image', 'dependency', 'terraform_provider', 'ansible_collection', 'model'
    )),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    version text NOT NULL CHECK (char_length(version) BETWEEN 1 AND 80),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    media_type text NOT NULL DEFAULT 'application/octet-stream'
        CHECK (char_length(media_type) BETWEEN 1 AND 120),
    UNIQUE (bundle_id, kind, name, version)
);
CREATE INDEX offline_bundle_items_bundle ON offline_bundle_items (bundle_id);

CREATE TABLE offline_bundle_imports (
    id uuid PRIMARY KEY,
    bundle_id uuid NOT NULL REFERENCES offline_bundles(id),
    outcome text NOT NULL CHECK (outcome IN ('ok', 'refused')),
    detail text NOT NULL DEFAULT '' CHECK (char_length(detail) <= 500),
    actor_id uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX offline_bundle_imports_bundle ON offline_bundle_imports (bundle_id, created_at DESC);

DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        FOREACH t IN ARRAY ARRAY[
            'offline_bundle_trust_keys',
            'offline_bundles',
            'offline_bundle_items',
            'offline_bundle_imports'
        ] LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_api', t);
        END LOOP;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        FOREACH t IN ARRAY ARRAY[
            'offline_bundle_trust_keys',
            'offline_bundles',
            'offline_bundle_items',
            'offline_bundle_imports'
        ] LOOP
            EXECUTE format('GRANT SELECT ON %I TO rackloom_worker', t);
        END LOOP;
    END IF;
END;
$$;
