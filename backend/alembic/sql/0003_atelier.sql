-- Atelier de templates (AT01) : brouillons d'auteur, revue, propositions d'extension et preuves
-- de vérification des composants. Le contenu soumis à la revue devient une version immuable du
-- registre (statut in_review) ; la publication reste un acte distinct du dépôt.

ALTER TABLE template_versions DROP CONSTRAINT template_versions_status_check;
ALTER TABLE template_versions ADD CONSTRAINT template_versions_status_check
    CHECK (status IN ('draft', 'lab_tested', 'in_review', 'returned', 'validated', 'published', 'deprecated',
                      'withdrawn'));
ALTER TABLE template_versions ADD COLUMN submitted_by uuid REFERENCES users(id);
ALTER TABLE template_versions ADD COLUMN submitted_at timestamptz;

ALTER TABLE template_events DROP CONSTRAINT template_events_action_check;
ALTER TABLE template_events ADD CONSTRAINT template_events_action_check
    CHECK (action IN ('loaded', 'submitted', 'returned', 'published', 'deprecated', 'withdrawn'));

-- Brouillon d'auteur : contrat candidat rackloom.template/v1, provenance de chaque valeur,
-- propositions de l'assistant en attente, points ouverts et sources consultées. Modifiable
-- seulement par son auteur tant qu'il n'est pas en revue.
CREATE TABLE template_drafts (
    id uuid PRIMARY KEY,
    slug text NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    version text NOT NULL CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    origin text NOT NULL CHECK (origin IN ('assistant', 'reference_host')),
    status text NOT NULL DEFAULT 'editing' CHECK (status IN ('editing', 'in_review', 'published', 'abandoned')),
    base_template_version_id uuid REFERENCES template_versions(id),
    content jsonb NOT NULL,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    proposals jsonb NOT NULL DEFAULT '[]'::jsonb,
    questions jsonb NOT NULL DEFAULT '[]'::jsonb,
    sources jsonb NOT NULL DEFAULT '[]'::jsonb,
    template_version_id uuid REFERENCES template_versions(id),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    row_version integer NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX template_drafts_open_slug ON template_drafts (slug) WHERE status IN ('editing', 'in_review');

ALTER TABLE template_versions ADD COLUMN draft_id uuid REFERENCES template_drafts(id);

-- Journal du brouillon (ajout seul) : tours de l'assistant (messages masqués), décisions de
-- l'auteur, vérifications, soumissions et décisions de revue, avec l'empreinte du contenu.
CREATE TABLE template_draft_events (
    id bigserial PRIMARY KEY,
    draft_id uuid NOT NULL REFERENCES template_drafts(id),
    action text NOT NULL,
    actor_type text NOT NULL CHECK (actor_type IN ('user', 'assistant', 'system')),
    actor_id text,
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_digest text NOT NULL,
    at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX template_draft_events_draft ON template_draft_events (draft_id, id);
CREATE TRIGGER template_draft_events_append_only
    BEFORE UPDATE OR DELETE ON template_draft_events
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

-- Proposition d'extension : texte à revoir (contrat, changement de rôle, tests, preuves attendues).
-- Jamais exécutée ni appliquée : elle entre au catalogue par revue, CI et test de laboratoire.
CREATE TABLE extension_proposals (
    id uuid PRIMARY KEY,
    draft_id uuid REFERENCES template_drafts(id),
    kind text NOT NULL CHECK (kind IN ('component_parameter', 'new_component')),
    subject text NOT NULL,
    summary text NOT NULL,
    proposal jsonb NOT NULL,
    origin text NOT NULL CHECK (origin IN ('assistant', 'author', 'reference_host')),
    status text NOT NULL DEFAULT 'to_review' CHECK (status IN ('to_review', 'rejected', 'accepted')),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Preuves de vérification d'une version de composant : enregistrées par le worker quand tous
-- les contrôles de santé d'un hôte réussissent. Le niveau de vérification en est calculé.
CREATE TABLE component_evidence (
    id bigserial PRIMARY KEY,
    component_version_id uuid NOT NULL REFERENCES component_versions(id),
    level text NOT NULL CHECK (level IN ('tested_locally', 'lab_validated')),
    source text NOT NULL,
    job_id uuid REFERENCES jobs(id),
    deployment_id uuid REFERENCES deployments(id),
    host_id uuid REFERENCES hosts(id),
    connector_mode text NOT NULL,
    checks jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX component_evidence_component ON component_evidence (component_version_id, recorded_at DESC);
CREATE TRIGGER component_evidence_append_only
    BEFORE UPDATE OR DELETE ON component_evidence
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        FOREACH t IN ARRAY ARRAY['template_drafts', 'template_draft_events', 'extension_proposals',
                                 'component_evidence'] LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_api', t);
        END LOOP;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        FOREACH t IN ARRAY ARRAY['template_drafts', 'template_draft_events', 'extension_proposals',
                                 'component_evidence'] LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_worker', t);
        END LOOP;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rackloom_worker;
    END IF;
END;
$$;
