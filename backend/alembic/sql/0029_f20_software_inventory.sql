-- F20 : inventaire logiciel exportable et fin de support.
-- Avis importés hors ligne (fixtures Ubuntu / USN) ; comparaison Debian/Ubuntu
-- (rétroportages). Un paquet n'est jamais marqué vulnérable sur la seule
-- comparaison avec la version amont. Export filtré par périmètre projet.

CREATE TABLE software_advisory_imports (
    id uuid PRIMARY KEY,
    source text NOT NULL CHECK (char_length(source) BETWEEN 1 AND 120),
    format text NOT NULL DEFAULT 'rackloom.software_advisories.v1'
        CHECK (format = 'rackloom.software_advisories.v1'),
    content_digest text NOT NULL UNIQUE CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    advisory_count integer NOT NULL CHECK (advisory_count >= 0),
    eol_count integer NOT NULL DEFAULT 0 CHECK (eol_count >= 0),
    -- Provenance explicite (simulé / fichier / flux) — jamais inventée.
    provenance jsonb NOT NULL,
    imported_by uuid REFERENCES users(id),
    imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX software_advisory_imports_at ON software_advisory_imports (imported_at DESC);

CREATE TABLE software_advisories (
    id uuid PRIMARY KEY,
    import_id uuid NOT NULL REFERENCES software_advisory_imports(id) ON DELETE CASCADE,
    advisory_id text NOT NULL CHECK (char_length(advisory_id) BETWEEN 1 AND 80),
    cve_ids text[] NOT NULL DEFAULT '{}',
    package_name text NOT NULL CHECK (package_name ~ '^[a-z0-9][a-z0-9+.-]{0,79}$'),
    -- Série Ubuntu (noble, jammy, …) à laquelle s'applique le correctif.
    series text NOT NULL CHECK (char_length(series) BETWEEN 1 AND 40),
    -- Version de distribution qui corrige (avec rétroportage). NULL = pas encore corrigé.
    fixed_version text CHECK (fixed_version IS NULL OR char_length(fixed_version) BETWEEN 1 AND 120),
    -- Version amont « corrigée » : documentation seulement ; jamais utilisée seule pour le verdict.
    upstream_fixed_version text
        CHECK (upstream_fixed_version IS NULL OR char_length(upstream_fixed_version) BETWEEN 1 AND 120),
    summary text NOT NULL CHECK (char_length(summary) BETWEEN 1 AND 500),
    published_on date,
    UNIQUE (import_id, advisory_id, package_name, series)
);
CREATE INDEX software_advisories_package ON software_advisories (package_name, series);
CREATE INDEX software_advisories_advisory ON software_advisories (advisory_id);

CREATE TABLE software_eol_products (
    id uuid PRIMARY KEY,
    import_id uuid NOT NULL REFERENCES software_advisory_imports(id) ON DELETE CASCADE,
    product text NOT NULL CHECK (char_length(product) BETWEEN 1 AND 80),
    series text NOT NULL CHECK (char_length(series) BETWEEN 1 AND 40),
    end_of_support date NOT NULL,
    source text NOT NULL CHECK (char_length(source) BETWEEN 1 AND 120),
    UNIQUE (import_id, product, series)
);
CREATE INDEX software_eol_products_lookup ON software_eol_products (product, series);

CREATE TABLE software_inventory_exports (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    actor_id uuid NOT NULL REFERENCES users(id),
    content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    host_count integer NOT NULL CHECK (host_count >= 0),
    package_count integer NOT NULL CHECK (package_count >= 0),
    finding_count integer NOT NULL CHECK (finding_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX software_inventory_exports_project
    ON software_inventory_exports (project_id, created_at DESC);

DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        FOREACH t IN ARRAY ARRAY[
            'software_advisory_imports',
            'software_advisories',
            'software_eol_products',
            'software_inventory_exports'
        ] LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_api', t);
        END LOOP;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        FOREACH t IN ARRAY ARRAY[
            'software_advisory_imports',
            'software_advisories',
            'software_eol_products',
            'software_inventory_exports'
        ] LOOP
            EXECUTE format('GRANT SELECT ON %I TO rackloom_worker', t);
        END LOOP;
    END IF;
END;
$$;
