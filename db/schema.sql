-- Friday Budgeting Pro — SQLite schema
-- Single-user, local-only personal finance database.

-- Plaid bank connections
CREATE TABLE IF NOT EXISTS bank_connections (
  id TEXT PRIMARY KEY,
  plaid_item_id TEXT UNIQUE,
  plaid_access_token_encrypted TEXT NOT NULL,
  institution_name TEXT,
  status TEXT DEFAULT 'active',  -- active | needs_reauth
  last_synced_at INTEGER
);

CREATE TABLE IF NOT EXISTS bank_accounts (
  id TEXT PRIMARY KEY,
  connection_id TEXT REFERENCES bank_connections(id),
  plaid_account_id TEXT UNIQUE,
  name TEXT,
  mask TEXT,
  type TEXT,
  subtype TEXT
);

-- Tracking structure (usually just one ledger called "Personal")
CREATE TABLE IF NOT EXISTS ledgers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS line_items (
  id TEXT PRIMARY KEY,
  ledger_id TEXT REFERENCES ledgers(id),
  name TEXT NOT NULL,
  item_type TEXT DEFAULT 'expense'  -- income | expense
);

-- Raw transactions from Plaid
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY,
  bank_account_id TEXT REFERENCES bank_accounts(id),
  plaid_transaction_id TEXT UNIQUE,
  date TEXT NOT NULL,
  merchant TEXT,
  amount REAL NOT NULL,
  plaid_category TEXT,
  pending INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transaction_entries (
  id TEXT PRIMARY KEY,
  transaction_id TEXT REFERENCES transactions(id),
  ledger_id TEXT REFERENCES ledgers(id),
  line_item_id TEXT REFERENCES line_items(id),
  amount REAL NOT NULL,
  source TEXT,       -- rule | llm | manual
  confidence REAL,
  reviewed INTEGER DEFAULT 0
);

-- Tier 1: deterministic rules (auto-created over time)
CREATE TABLE IF NOT EXISTS routing_rules (
  id TEXT PRIMARY KEY,
  merchant_pattern TEXT,
  line_item_id TEXT REFERENCES line_items(id)
);

-- Tier 2: natural-language hints fed to the LLM
CREATE TABLE IF NOT EXISTS classification_hints (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL
);

-- Plaid sync cursors
CREATE TABLE IF NOT EXISTS sync_cursors (
  connection_id TEXT PRIMARY KEY REFERENCES bank_connections(id),
  cursor TEXT,
  last_synced_at INTEGER
);

-- UI auth: single-row app config (single-user system)
-- TODO: existing DBs will not have the notification_channel column added here;
--       a migration (ALTER TABLE app_config ADD COLUMN ...) is needed for them.
CREATE TABLE IF NOT EXISTS app_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  ui_password_hash TEXT,       -- argon2id hash
  ui_password_set_at INTEGER,
  notification_channel TEXT DEFAULT 'in_ui'  -- openclaw_chat | macos | in_ui
);

-- UI session cookies (server-side store, survives restarts)
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,         -- session token (random 32 bytes hex)
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  user_agent TEXT
);

-- Notification log — every send() call writes a row; the UI reads this for banners
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  message TEXT NOT NULL,
  urgency TEXT DEFAULT 'normal',  -- normal | high
  created_at INTEGER NOT NULL,
  delivered_via TEXT,             -- openclaw_chat | macos | in_ui
  read INTEGER DEFAULT 0
);

-- Auto-promoted rules audit log (tracks which routing_rules were auto-created and from which transactions)
-- TODO: existing DBs will not have this table; run the schema again (init_db) or issue a one-off
--       CREATE TABLE migration to add it to production databases.
CREATE TABLE IF NOT EXISTS auto_promoted_rules_log (
  id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL REFERENCES routing_rules(id) ON DELETE CASCADE,
  merchant TEXT NOT NULL,
  line_item_id TEXT NOT NULL,
  source_transaction_ids TEXT NOT NULL,  -- JSON array of transaction ids
  created_at INTEGER NOT NULL
);

-- Login attempt log for rate limiting
CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempted_at INTEGER NOT NULL,
  success INTEGER NOT NULL     -- 0 = failed, 1 = success
);
