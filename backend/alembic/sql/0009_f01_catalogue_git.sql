-- F01 : catalogue Git avec promotion.
-- Publication (registre) et promotion (disponibilité par kind d'environnement) sont distinctes.
-- Une source Git doit être épinglée à un commit (ou empreinte d'artefact) et revue avant publication.

CREATE TABLE catalog_source_pins (
    id uuid PRIMARY KEY,
    source_kind text NOT NULL CHECK (source_kind IN ('filesystem', 'git_commit', 'artifact_digest')),
    -- Chemin local de labo ou libellé d'artefact ; jamais d'hôte réel dans le dépôt.
    repo_label text NOT NULL,
    commit_sha text CHECK (commit_sha IS NULL OR commit_sha ~ '^[0-9a-f]{40}$'),
    artifact_digest text CHECK (artifact_digest IS NULL OR artifact_digest ~ '^[0-9a-f]{64}$'),
    reviewed boolean NOT NULL DEFAULT false,
    reviewed_by uuid REFERENCES users(id),
    reviewed_at timestamptz,
    review_reason text,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT catalog_source_pins_pin CHECK (
        (source_kind = 'filesystem')
        OR (source_kind = 'git_commit' AND commit_sha IS NOT NULL)
        OR (source_kind = 'artifact_digest' AND artifact_digest IS NOT NULL)
    )
);

ALTER TABLE template_versions ADD COLUMN source_pin_id uuid REFERENCES catalog_source_pins(id);
ALTER TABLE component_versions ADD COLUMN source_pin_id uuid REFERENCES catalog_source_pins(id);
ALTER TABLE catalog_artifacts ADD COLUMN source_pin_id uuid REFERENCES catalog_source_pins(id);

CREATE TABLE template_promotions (
    id uuid PRIMARY KEY,
    template_version_id uuid NOT NULL REFERENCES template_versions(id),
    environment_kind text NOT NULL CHECK (environment_kind IN ('dev', 'test', 'preprod', 'prod')),
    promoted_by uuid REFERENCES users(id),
    promoted_at timestamptz NOT NULL DEFAULT now(),
    reason text NOT NULL,
    UNIQUE (template_version_id, environment_kind)
);
CREATE INDEX template_promotions_kind ON template_promotions (environment_kind, promoted_at DESC);

ALTER TABLE template_events DROP CONSTRAINT template_events_action_check;
ALTER TABLE template_events ADD CONSTRAINT template_events_action_check
    CHECK (action IN ('loaded', 'submitted', 'returned', 'published', 'deprecated', 'withdrawn',
                      'promoted', 'pin_reviewed'));

-- Les versions déjà publiées restent disponibles partout (rétrocompatibilité lot 1 / A).
INSERT INTO template_promotions (id, template_version_id, environment_kind, promoted_by, reason)
SELECT gen_random_uuid(), tv.id, k.kind, tv.published_by,
       'promotion initiale (migration F01) : versions déjà publiées'
FROM template_versions tv
CROSS JOIN (VALUES ('dev'), ('test'), ('preprod'), ('prod')) AS k(kind)
WHERE tv.status = 'published'
ON CONFLICT DO NOTHING;
