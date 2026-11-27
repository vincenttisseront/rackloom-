-- F22 : dossiers de diagnostic partageables.
-- Export filtré (période, symptômes, preuves, événements, données manquantes).
-- Stocké en JSONB applicatif : l'API ne peut pas écrire le volume artefacts
-- (monté en lecture seule ; classe B = worker écrit / API lit). Les preuves
-- référencées restent des métadonnées d'artefacts de classe B du worker.
-- Droits revérifiés à la génération et au téléchargement.
-- Aucun secret ni donnée hors périmètre dans l'export ni dans le résumé.

CREATE TABLE diagnostic_packs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    change_id uuid NOT NULL REFERENCES changes(id),
    -- Optionnel : copie miroir en artefact B si un worker l'a matérialisée.
    artifact_id uuid REFERENCES artifacts(id),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    period_start timestamptz NOT NULL,
    period_end timestamptz NOT NULL,
    symptoms jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    missing jsonb NOT NULL DEFAULT '[]'::jsonb,
    summary jsonb,
    -- Export complet filtré (source de vérité du téléchargement API).
    payload jsonb NOT NULL,
    event_count integer NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    evidence_count integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    digest text NOT NULL,
    schema_version text NOT NULL DEFAULT 'rackloom.diagnostic_pack/v1'
);
CREATE INDEX diagnostic_packs_change ON diagnostic_packs (change_id, created_at DESC);
CREATE INDEX diagnostic_packs_project ON diagnostic_packs (project_id, created_at DESC);
CREATE INDEX diagnostic_packs_artifact ON diagnostic_packs (artifact_id)
    WHERE artifact_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT ON diagnostic_packs TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON diagnostic_packs TO rackloom_worker;
    END IF;
END $$;
