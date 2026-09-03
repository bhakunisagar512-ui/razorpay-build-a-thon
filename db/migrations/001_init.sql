CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now());

DO $$ BEGIN
  CREATE TYPE failure_cause AS ENUM (
    'BD_INSUFFICIENT_FUNDS','BD_USER_ABANDONED_OTP','TD_ISSUER_DOWN',
    'TD_PSP_TIMEOUT','CARD_EXPIRED','RISK_BLOCKED','AMBIGUOUS');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE plan_status AS ENUM ('active','succeeded','exhausted','aborted');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS webhook_event (
  event_id TEXT PRIMARY KEY,
  raw JSONB NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS failed_payment (
  id TEXT PRIMARY KEY,
  order_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  amount_minor BIGINT NOT NULL,
  currency CHAR(3) NOT NULL DEFAULT 'INR',
  method TEXT NOT NULL,
  gw_error_code TEXT, gw_error_source TEXT, gw_error_step TEXT,
  instrument_hint TEXT NOT NULL DEFAULT 'na',
  cause failure_cause NOT NULL,
  cause_conf NUMERIC(4,3) NOT NULL,
  cause_stage TEXT NOT NULL DEFAULT 'rules',
  arm TEXT NOT NULL CHECK (arm IN ('treatment','holdout')),
  failed_at TIMESTAMPTZ NOT NULL);
CREATE INDEX IF NOT EXISTS idx_fp_order ON failed_payment (order_id);
CREATE INDEX IF NOT EXISTS idx_fp_cust  ON failed_payment (customer_id, failed_at DESC);

CREATE TABLE IF NOT EXISTS recovery_plan (
  id BIGSERIAL PRIMARY KEY,
  payment_id TEXT NOT NULL REFERENCES failed_payment(id),
  policy_ver TEXT NOT NULL,
  steps JSONB NOT NULL,
  status plan_status NOT NULL DEFAULT 'active',
  abort_reason TEXT);

CREATE TABLE IF NOT EXISTS recovery_attempt (
  id BIGSERIAL PRIMARY KEY,
  plan_id BIGINT NOT NULL REFERENCES recovery_plan(id),
  step_idx INT NOT NULL,
  action TEXT NOT NULL,
  channel TEXT, rail TEXT,
  executed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  outcome TEXT NOT NULL,
  suppressed_by TEXT,
  new_payment_id TEXT,
  cost_minor BIGINT NOT NULL DEFAULT 0,
  UNIQUE (plan_id, step_idx));

CREATE TABLE IF NOT EXISTS recovery_outcome (
  payment_id TEXT PRIMARY KEY REFERENCES failed_payment(id),
  recovered BOOLEAN NOT NULL,
  recovered_at TIMESTAMPTZ,
  recovered_minor BIGINT NOT NULL DEFAULT 0,
  cost_minor BIGINT NOT NULL DEFAULT 0,
  attempts_used INT NOT NULL DEFAULT 0,
  attributed_step INT);

CREATE TABLE IF NOT EXISTS audit_log (
  seq BIGSERIAL PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  payload JSONB NOT NULL,
  policy_ver TEXT,
  prev_hash CHAR(64) NOT NULL,
  hash CHAR(64) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log (entity_type, entity_id, seq);
