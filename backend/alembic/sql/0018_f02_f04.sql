-- F02 : compositions multi-VM (membre de composition sur l'hôte).
-- F04 : graphe de dépendances (nœuds, arêtes, provenance) et snapshots de reconstruction.

ALTER TABLE hosts
    ADD COLUMN IF NOT EXISTS composition_member text
        CHECK (composition_member IS NULL OR char_length(composition_member) BETWEEN 1 AND 40);

CREATE INDEX IF NOT EXISTS hosts_composition_member
    ON hosts (deployment_id, composition_member)
    WHERE composition_member IS NOT NULL;

-- Instantané de reconstruction du graphe (lecture ; aucune écriture fournisseur).
CREATE TABLE dependency_graph_builds (
    id uuid PRIMARY KEY,
    built_at timestamptz NOT NULL DEFAULT now(),
    built_by text NOT NULL CHECK (char_length(built_by) BETWEEN 1 AND 80),
    node_count integer NOT NULL DEFAULT 0 CHECK (node_count >= 0),
    edge_count integer NOT NULL DEFAULT 0 CHECK (edge_count >= 0),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500)
);

CREATE TABLE dependency_nodes (
    id uuid PRIMARY KEY,
    build_id uuid NOT NULL REFERENCES dependency_graph_builds(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN (
        'vm', 'template', 'network', 'datastore', 'certificate', 'service', 'cluster', 'image'
    )),
    stable_id text NOT NULL CHECK (char_length(stable_id) BETWEEN 1 AND 200),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    -- NULL pour les ressources d'inventaire partagées (réseau, datastore, …).
    project_id uuid REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (build_id, kind, stable_id)
);
CREATE INDEX dependency_nodes_kind ON dependency_nodes (build_id, kind);
CREATE INDEX dependency_nodes_project ON dependency_nodes (build_id, project_id)
    WHERE project_id IS NOT NULL;

CREATE TABLE dependency_edges (
    id uuid PRIMARY KEY,
    build_id uuid NOT NULL REFERENCES dependency_graph_builds(id) ON DELETE CASCADE,
    from_node_id uuid NOT NULL REFERENCES dependency_nodes(id) ON DELETE CASCADE,
    to_node_id uuid NOT NULL REFERENCES dependency_nodes(id) ON DELETE CASCADE,
    relation text NOT NULL CHECK (relation IN (
        'uses_network', 'uses_datastore', 'uses_cluster', 'uses_image',
        'uses_template', 'provides_service', 'secured_by', 'depends_on_member',
        'inventory_parent'
    )),
    -- confirmée (révision / hôte), inférée (parent inventaire), inconnue.
    provenance text NOT NULL CHECK (provenance IN ('confirmed', 'inferred', 'unknown')),
    source text NOT NULL CHECK (char_length(source) BETWEEN 1 AND 80),
    -- Périmètre de l'arête : filtrage impact (404 hors projet). NULL = arête d'inventaire.
    project_id uuid REFERENCES projects(id),
    UNIQUE (build_id, from_node_id, to_node_id, relation)
);
CREATE INDEX dependency_edges_to ON dependency_edges (build_id, to_node_id, relation);
CREATE INDEX dependency_edges_from ON dependency_edges (build_id, from_node_id, relation);
CREATE INDEX dependency_edges_project ON dependency_edges (build_id, project_id)
    WHERE project_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, UPDATE ON hosts TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON dependency_graph_builds TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON dependency_nodes TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE, DELETE ON dependency_edges TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT, UPDATE ON hosts TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE, DELETE ON dependency_graph_builds TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE, DELETE ON dependency_nodes TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE, DELETE ON dependency_edges TO rackloom_worker;
    END IF;
END $$;
