-- F47–F50 : runbooks approuvés, routage des modèles, préférences de projet.
-- Aucun secret ; versions immuables une fois publiées (ajout seul pour runbooks et préférences).

CREATE TABLE runbooks (
    id uuid PRIMARY KEY,
    slug text NOT NULL,
    version text NOT NULL,
    status text NOT NULL CHECK (status IN ('draft', 'approved', 'withdrawn')),
    title text NOT NULL,
    digest text NOT NULL,
    triggers jsonb NOT NULL DEFAULT '{}'::jsonb,
    body jsonb NOT NULL,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (slug, version)
);
CREATE INDEX runbooks_approved ON runbooks (status) WHERE status = 'approved';
CREATE TRIGGER runbooks_append_only
    BEFORE UPDATE OR DELETE ON runbooks
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

-- Exception : retrait (status → withdrawn) autorisé via fonction dédiée côté appli
-- en désactivant le trigger temporairement n'est pas souhaité ; on garde append-only
-- et on insère une nouvelle version « withdrawn » si besoin. Pour le lot actuel :
-- seule l'insertion d'une version approved est utilisée.

CREATE TABLE project_preference_versions (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    version integer NOT NULL,
    digest text NOT NULL,
    preferences jsonb NOT NULL,
    reason text NOT NULL,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, version)
);
CREATE INDEX project_preference_versions_current
    ON project_preference_versions (project_id, version DESC);
CREATE TRIGGER project_preference_versions_append_only
    BEFORE UPDATE OR DELETE ON project_preference_versions
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT ON runbooks TO rackloom_api;
        GRANT SELECT, INSERT ON project_preference_versions TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON runbooks TO rackloom_worker;
        GRANT SELECT ON project_preference_versions TO rackloom_worker;
    END IF;
END $$;
