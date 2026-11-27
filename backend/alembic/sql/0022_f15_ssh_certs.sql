-- F15 : certificats SSH à courte durée.
-- CA de test (clé de signature scellée hors table) ; émissions auditées par job ;
-- identité et périmètre exprimés en principals OpenSSH. Aucune clé privée ici.

CREATE TABLE ssh_cas (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE
        CHECK (char_length(slug) BETWEEN 1 AND 40 AND slug ~ '^[a-z][a-z0-9-]*$'),
    public_key text NOT NULL CHECK (char_length(public_key) BETWEEN 32 AND 1000),
    fingerprint text NOT NULL CHECK (char_length(fingerprint) BETWEEN 16 AND 80),
    purpose text NOT NULL DEFAULT 'lab'
        CHECK (purpose IN ('lab', 'appliance')),
    secret_ref text NOT NULL CHECK (char_length(secret_ref) BETWEEN 12 AND 200),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    rotated_at timestamptz
);
CREATE UNIQUE INDEX ssh_cas_one_active_lab
    ON ssh_cas (purpose) WHERE active AND purpose = 'lab';

CREATE SEQUENCE ssh_certificate_serial START 1;

CREATE TABLE ssh_certificate_issues (
    id uuid PRIMARY KEY,
    ca_id uuid NOT NULL REFERENCES ssh_cas(id),
    job_id uuid REFERENCES jobs(id),
    serial bigint NOT NULL,
    key_id text NOT NULL CHECK (char_length(key_id) BETWEEN 1 AND 200),
    identity text NOT NULL CHECK (char_length(identity) BETWEEN 1 AND 80),
    principals text[] NOT NULL CHECK (cardinality(principals) BETWEEN 1 AND 64),
    valid_after timestamptz NOT NULL,
    valid_before timestamptz NOT NULL,
    public_key_fingerprint text NOT NULL CHECK (char_length(public_key_fingerprint) BETWEEN 16 AND 80),
    issued_at timestamptz NOT NULL DEFAULT now(),
    CHECK (valid_before > valid_after),
    UNIQUE (ca_id, serial)
);
CREATE INDEX ssh_certificate_issues_job ON ssh_certificate_issues (job_id);
CREATE INDEX ssh_certificate_issues_ca ON ssh_certificate_issues (ca_id, issued_at DESC);

CREATE TRIGGER ssh_certificate_issues_append_only
    BEFORE UPDATE OR DELETE ON ssh_certificate_issues
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ssh_cas TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ssh_certificate_issues TO rackloom_api;
        GRANT USAGE, SELECT ON SEQUENCE ssh_certificate_serial TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ssh_cas TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ssh_certificate_issues TO rackloom_worker;
        GRANT USAGE, SELECT ON SEQUENCE ssh_certificate_serial TO rackloom_worker;
    END IF;
END;
$$;
