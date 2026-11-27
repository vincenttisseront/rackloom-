-- F30 : SDK d'adaptateurs — registre de paquets avec provenance et approbation.
-- Aucune installation depuis l'UI sans statut approved + digest de confiance.
-- Approuver n'installe pas ; installer refuse si non approuvé (adapter_not_approved).

CREATE TABLE adapter_trust_digests (
    id uuid PRIMARY KEY,
    content_digest text NOT NULL UNIQUE CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    label text NOT NULL CHECK (char_length(label) BETWEEN 1 AND 200),
    active boolean NOT NULL DEFAULT true,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE adapter_packages (
    id uuid PRIMARY KEY,
    slug text NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,62}[a-z0-9]$'),
    version text NOT NULL CHECK (version ~ '^\d+\.\d+\.\d+$'),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    kind text NOT NULL CHECK (kind IN ('reference', 'proxmox', 'opentofu', 'awx', 'other')),
    -- Cycle : submitted → approved → installed | rejected
    status text NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'approved', 'installed', 'rejected')),
    content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    manifest jsonb NOT NULL,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    submitted_by uuid REFERENCES users(id),
    submitted_at timestamptz NOT NULL DEFAULT now(),
    approved_by uuid REFERENCES users(id),
    approved_at timestamptz,
    installed_connector_id uuid REFERENCES connectors(id),
    installed_at timestamptz,
    rejected_by uuid REFERENCES users(id),
    rejected_at timestamptz,
    reject_reason text CHECK (reject_reason IS NULL OR char_length(reject_reason) BETWEEN 1 AND 500),
    UNIQUE (slug, version)
);
CREATE INDEX adapter_packages_status ON adapter_packages (status, submitted_at DESC);
CREATE INDEX adapter_packages_digest ON adapter_packages (content_digest);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON adapter_trust_digests TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON adapter_packages TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON adapter_trust_digests TO rackloom_worker;
        GRANT SELECT ON adapter_packages TO rackloom_worker;
    END IF;
END $$;
