-- F37 : disjoncteur par connecteur (docs/backlog.md).
-- États : closed (écritures admises) / open (nouvelles écritures bloquées à l'admission).
-- Les colonnes d'ouverture ne sont renseignées que lorsque le disjoncteur est ouvert.

ALTER TABLE connectors
    ADD COLUMN circuit_state text NOT NULL DEFAULT 'closed'
        CHECK (circuit_state IN ('closed', 'open')),
    ADD COLUMN consecutive_failures integer NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    ADD COLUMN circuit_opened_at timestamptz,
    ADD COLUMN circuit_opened_reason text
        CHECK (circuit_opened_reason IS NULL
               OR length(btrim(circuit_opened_reason)) BETWEEN 3 AND 500),
    ADD CONSTRAINT connectors_circuit_open_fields CHECK (
        (circuit_state = 'closed'
         AND circuit_opened_at IS NULL
         AND circuit_opened_reason IS NULL)
        OR
        (circuit_state = 'open'
         AND circuit_opened_at IS NOT NULL
         AND circuit_opened_reason IS NOT NULL)
    );

CREATE INDEX connectors_circuit_open ON connectors (kind) WHERE circuit_state = 'open';
