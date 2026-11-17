"""Phase-3 data model: master data, processing, append-only audit chain.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ACTIONS = "('approve', 'clarify', 'block')"
INVOICE_STATUSES = (
    "('received', 'processing', 'pending_approval', 'approved_for_payment', "
    "'clarification_requested', 'blocked', 'awaiting_quota', 'failed')"
)

MASTER = [
    """
    CREATE TABLE vendors (
        id text PRIMARY KEY,
        name text NOT NULL,
        vat_id text NOT NULL CONSTRAINT vendors_vat_id_key UNIQUE,
        country text NOT NULL,
        address text NOT NULL
    )
    """,
    # Vendor lookup falls back to a trigram match on the name (B1 retrieval).
    "CREATE INDEX vendors_name_trgm_idx ON vendors USING gin (name gin_trgm_ops)",
    """
    CREATE TABLE vendor_bank_accounts (
        id bigserial PRIMARY KEY,
        vendor_id text NOT NULL REFERENCES vendors (id),
        iban text NOT NULL,
        valid_from date NOT NULL,
        valid_to date,
        CONSTRAINT vendor_bank_accounts_natural_key UNIQUE (vendor_id, iban, valid_from),
        CONSTRAINT vendor_bank_accounts_valid_range
            CHECK (valid_to IS NULL OR valid_to >= valid_from)
    )
    """,
    # Vendor lookup by exact IBAN.
    "CREATE INDEX vendor_bank_accounts_iban_idx ON vendor_bank_accounts (iban)",
    """
    CREATE TABLE purchase_orders (
        id text PRIMARY KEY,
        vendor_id text NOT NULL REFERENCES vendors (id),
        currency text NOT NULL,
        issue_date date NOT NULL,
        total_net numeric(14, 2) NOT NULL
    )
    """,
    "CREATE INDEX purchase_orders_vendor_id_idx ON purchase_orders (vendor_id)",
    """
    CREATE TABLE po_lines (
        po_id text NOT NULL REFERENCES purchase_orders (id) ON DELETE CASCADE,
        line_no integer NOT NULL,
        sku text NOT NULL,
        description text NOT NULL,
        quantity numeric NOT NULL,
        unit_price numeric(14, 2) NOT NULL,
        PRIMARY KEY (po_id, line_no)
    )
    """,
    """
    CREATE TABLE contracts (
        id text PRIMARY KEY,
        vendor_id text NOT NULL REFERENCES vendors (id),
        currency text NOT NULL,
        valid_from date NOT NULL,
        valid_to date NOT NULL,
        CONSTRAINT contracts_valid_range CHECK (valid_to >= valid_from)
    )
    """,
    "CREATE INDEX contracts_vendor_id_idx ON contracts (vendor_id)",
    """
    CREATE TABLE contract_prices (
        contract_id text NOT NULL REFERENCES contracts (id) ON DELETE CASCADE,
        sku text NOT NULL,
        unit_price numeric(14, 2) NOT NULL,
        PRIMARY KEY (contract_id, sku)
    )
    """,
    """
    CREATE TABLE contract_clauses (
        contract_id text NOT NULL REFERENCES contracts (id) ON DELETE CASCADE,
        clause_no text NOT NULL,
        title text NOT NULL,
        "text" text NOT NULL,
        tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || "text")) STORED,
        PRIMARY KEY (contract_id, clause_no)
    )
    """,
    "CREATE INDEX contract_clauses_tsv_idx ON contract_clauses USING gin (tsv)",
    """
    CREATE TABLE vat_rates (
        country text PRIMARY KEY,
        rate_pct numeric(5, 2) NOT NULL
    )
    """,
    # Small policy values loaded with the master data (e.g. po_tolerance_pct).
    """
    CREATE TABLE settings_kv (
        key text PRIMARY KEY,
        value text NOT NULL
    )
    """,
]

PROCESSING = [
    f"""
    CREATE TABLE invoices (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        sha256 text NOT NULL CONSTRAINT invoices_sha256_key UNIQUE
            CONSTRAINT invoices_sha256_hex CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
        filename text NOT NULL,
        pdf bytea NOT NULL,
        size_bytes integer NOT NULL CHECK (size_bytes > 0),
        page_count integer NOT NULL CHECK (page_count > 0),
        status text NOT NULL DEFAULT 'received'
            CONSTRAINT invoices_status_check CHECK (status IN {INVOICE_STATUSES}),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        -- Optimistic concurrency for If-Match on the approval endpoint.
        version integer NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX invoices_status_idx ON invoices (status)",
    """
    CREATE TABLE processing_runs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        invoice_id uuid NOT NULL REFERENCES invoices (id),
        provider text NOT NULL,
        extraction_model text NOT NULL,
        -- NULL when no rule raised a flag, so the agent never ran.
        agent_model text,
        prompt_version text NOT NULL,
        started_at timestamptz NOT NULL DEFAULT now(),
        finished_at timestamptz
    )
    """,
    "CREATE INDEX processing_runs_invoice_id_idx ON processing_runs (invoice_id)",
    # Deliberately not unique: duplicates must be storable so the search can find them.
    """
    CREATE TABLE extractions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        invoice_id uuid NOT NULL REFERENCES invoices (id),
        run_id uuid NOT NULL REFERENCES processing_runs (id),
        fields jsonb NOT NULL,
        vendor_vat_id text,
        invoice_number text,
        invoice_number_norm text,
        total numeric(14, 2),
        issue_date date,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX extractions_invoice_id_idx ON extractions (invoice_id)",
    # Duplicate search: exact (vendor, normalised number), then amount within a date window,
    # then a trigram match on the number.
    """
    CREATE INDEX extractions_vendor_number_idx
        ON extractions (vendor_vat_id, invoice_number_norm)
    """,
    "CREATE INDEX extractions_total_date_idx ON extractions (total, issue_date)",
    """
    CREATE INDEX extractions_number_norm_trgm_idx
        ON extractions USING gin (invoice_number_norm gin_trgm_ops)
    """,
    """
    CREATE TABLE flags (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        invoice_id uuid NOT NULL REFERENCES invoices (id),
        run_id uuid NOT NULL REFERENCES processing_runs (id),
        code text NOT NULL,
        severity text NOT NULL,
        evidence jsonb NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX flags_invoice_id_idx ON flags (invoice_id)",
    f"""
    CREATE TABLE proposals (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        invoice_id uuid NOT NULL REFERENCES invoices (id),
        run_id uuid NOT NULL REFERENCES processing_runs (id),
        rule_floor text NOT NULL
            CONSTRAINT proposals_rule_floor_check CHECK (rule_floor IN {ACTIONS}),
        -- NULL when the agent did not run (no rule raised a flag).
        agent_action text CONSTRAINT proposals_agent_action_check CHECK (agent_action IN {ACTIONS}),
        final_action text NOT NULL
            CONSTRAINT proposals_final_action_check CHECK (final_action IN {ACTIONS}),
        rationale text NOT NULL,
        evidence_refs jsonb NOT NULL DEFAULT '[]',
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX proposals_invoice_id_idx ON proposals (invoice_id)",
    f"""
    CREATE TABLE approvals (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        proposal_id uuid NOT NULL REFERENCES proposals (id),
        decision text NOT NULL CONSTRAINT approvals_decision_check CHECK (decision IN {ACTIONS}),
        decided_by text NOT NULL,
        note text,
        decided_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX approvals_proposal_id_idx ON approvals (proposal_id)",
    # The source of truth for quota/spend: one row per provider call.
    """
    CREATE TABLE llm_calls (
        id bigserial PRIMARY KEY,
        invoice_id uuid REFERENCES invoices (id),
        provider text NOT NULL,
        model text NOT NULL,
        purpose text NOT NULL,
        input_tokens integer NOT NULL DEFAULT 0,
        output_tokens integer NOT NULL DEFAULT 0,
        latency_ms integer,
        status text NOT NULL,
        error_class text,
        demo_token_hash text,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # Per-provider daily soft cap.
    "CREATE INDEX llm_calls_provider_created_at_idx ON llm_calls (provider, created_at)",
    # Per-demo-token daily sub-cap.
    """
    CREATE INDEX llm_calls_demo_token_created_at_idx ON llm_calls (demo_token_hash, created_at)
        WHERE demo_token_hash IS NOT NULL
    """,
    """
    CREATE TABLE jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        invoice_id uuid NOT NULL REFERENCES invoices (id),
        stage text NOT NULL,
        status text NOT NULL DEFAULT 'queued'
            CONSTRAINT jobs_status_check CHECK (status IN ('queued', 'running', 'done', 'failed')),
        attempts integer NOT NULL DEFAULT 0,
        lease_owner text,
        lease_expires_at timestamptz,
        heartbeat_at timestamptz,
        last_error text,
        -- Output of each completed stage, so a restart resumes without paying twice.
        stage_outputs jsonb NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # The same invoice can never be enqueued twice while a job for it is still live.
    """
    CREATE UNIQUE INDEX jobs_one_active_per_invoice ON jobs (invoice_id)
        WHERE status IN ('queued', 'running')
    """,
    # Claim queue (FOR UPDATE SKIP LOCKED) and the expired-lease sweep.
    "CREATE INDEX jobs_queued_idx ON jobs (created_at) WHERE status = 'queued'",
    "CREATE INDEX jobs_running_lease_idx ON jobs (lease_expires_at) WHERE status = 'running'",
    """
    CREATE TABLE budget_state (
        provider text PRIMARY KEY,
        exhausted_until timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
]

AUDIT = [
    """
    CREATE TABLE audit_events (
        seq bigserial PRIMARY KEY,
        occurred_at timestamptz NOT NULL DEFAULT now(),
        actor text NOT NULL,
        event_type text NOT NULL,
        entity_type text NOT NULL,
        entity_id text NOT NULL,
        payload jsonb NOT NULL DEFAULT '{}',
        -- Unique too: two events claiming the same predecessor would be a forked chain.
        prev_hash text NOT NULL CONSTRAINT audit_events_prev_hash_key UNIQUE,
        hash text NOT NULL CONSTRAINT audit_events_hash_key UNIQUE
    )
    """,
    "CREATE INDEX audit_events_entity_idx ON audit_events (entity_type, entity_id)",
    # Rejects UPDATE/DELETE/TRUNCATE for every role, the owner included. The owner can still
    # disable the trigger, which is why the chain is tamper-evident rather than tamper-proof.
    """
    CREATE FUNCTION audit_events_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'audit_events is append-only: % is not allowed', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END
    $$
    """,
    """
    CREATE TRIGGER audit_events_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_reject_mutation()
    """,
    """
    CREATE TRIGGER audit_events_no_truncate
        BEFORE TRUNCATE ON audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION audit_events_reject_mutation()
    """,
]

# Children before parents.
DROP_TABLES = [
    "audit_events",
    "budget_state",
    "jobs",
    "llm_calls",
    "approvals",
    "proposals",
    "flags",
    "extractions",
    "processing_runs",
    "invoices",
    "settings_kv",
    "vat_rates",
    "contract_clauses",
    "contract_prices",
    "contracts",
    "po_lines",
    "purchase_orders",
    "vendor_bank_accounts",
    "vendors",
]


def upgrade() -> None:
    for statement in MASTER + PROCESSING + AUDIT:
        op.execute(statement)


def downgrade() -> None:
    for table in DROP_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS audit_events_reject_mutation()")
