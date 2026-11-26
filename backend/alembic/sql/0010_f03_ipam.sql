-- F03 : réservations IPAM/DNS (adaptateur fictif + cycle préparation → réservation → confirmation → libération).

CREATE TABLE ipam_pools (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    title text NOT NULL,
    cidr text NOT NULL,
    dns_suffix text NOT NULL DEFAULT 'lab.demo.invalid',
    connector_slug text NOT NULL DEFAULT 'ipam-fake',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ipam_reservations (
    id uuid PRIMARY KEY,
    pool_id uuid NOT NULL REFERENCES ipam_pools(id),
    address inet NOT NULL,
    dns_name text NOT NULL,
    state text NOT NULL
        CHECK (state IN ('prepared', 'reserved', 'confirmed', 'released', 'held_uncertain')),
    project_id uuid REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    change_id uuid REFERENCES changes(id),
    job_id uuid REFERENCES jobs(id),
    preview_only boolean NOT NULL DEFAULT false,
    prepared_at timestamptz NOT NULL DEFAULT now(),
    reserved_at timestamptz,
    confirmed_at timestamptz,
    released_at timestamptz,
    hold_reason text,
    UNIQUE (pool_id, address)
);
CREATE UNIQUE INDEX ipam_reservations_dns_active
    ON ipam_reservations (pool_id, dns_name)
    WHERE state IN ('prepared', 'reserved', 'confirmed', 'held_uncertain');
CREATE INDEX ipam_reservations_change ON ipam_reservations (change_id);

INSERT INTO ipam_pools (id, slug, title, cidr, dns_suffix, connector_slug)
VALUES (
    '01900000-0000-7000-8000-000000000301',
    'lab-demo',
    'Plage de laboratoire (documentation RFC 5737)',
    '192.0.2.0/24',
    'lab.demo.invalid',
    'ipam-fake'
);
