PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins (
 id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, instrument TEXT NOT NULL,
 bio TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '',
 sort_order INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, starts_at INTEGER NOT NULL,
 ends_at INTEGER NOT NULL, venue TEXT NOT NULL, address TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', admission TEXT NOT NULL CHECK(admission IN ('free','paid','external')),
 price_cents INTEGER NOT NULL DEFAULT 0 CHECK(price_cents >= 0),
 capacity INTEGER NOT NULL DEFAULT 0 CHECK(capacity >= 0),
 sales_end INTEGER NOT NULL, sales_open INTEGER NOT NULL DEFAULT 0,
 external_url TEXT NOT NULL DEFAULT '', refund_policy TEXT NOT NULL DEFAULT '',
 cover TEXT NOT NULL DEFAULT '', recap TEXT NOT NULL DEFAULT '',
 setlist TEXT NOT NULL DEFAULT '', gallery TEXT NOT NULL DEFAULT '', video_url TEXT NOT NULL DEFAULT '',
 published INTEGER NOT NULL DEFAULT 0, is_test INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_members (
 event_id INTEGER NOT NULL REFERENCES events(id), member_id INTEGER NOT NULL REFERENCES members(id),
 PRIMARY KEY(event_id,member_id)
);
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY, public_id TEXT UNIQUE NOT NULL, access_hash TEXT NOT NULL,
 request_key TEXT UNIQUE NOT NULL, event_id INTEGER NOT NULL REFERENCES events(id),
 buyer TEXT NOT NULL, contact TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 6),
 unit_cents INTEGER NOT NULL CHECK(unit_cents > 0), status TEXT NOT NULL
 CHECK(status IN ('pending','review','paid','expired','cancelled','refunded')),
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, review_until INTEGER,
 reported_at INTEGER, payment_note TEXT NOT NULL DEFAULT '', receipt_ref TEXT UNIQUE,
 policy_snapshot TEXT NOT NULL, payee_snapshot TEXT NOT NULL DEFAULT '', payment_image_snapshot TEXT NOT NULL DEFAULT '',
 confirmed_at INTEGER, refunded_at INTEGER, refund_ref TEXT
);
CREATE INDEX IF NOT EXISTS orders_event_status ON orders(event_id,status);
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
 code TEXT UNIQUE NOT NULL, used_at INTEGER, used_by INTEGER REFERENCES admins(id)
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, admin_id INTEGER REFERENCES admins(id),
 action TEXT NOT NULL, object_id INTEGER, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rate_limits (
 key TEXT PRIMARY KEY, started_at INTEGER NOT NULL, attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
 token_hash TEXT PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES admins(id),
 expires_at INTEGER NOT NULL, last_seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_sessions_admin ON admin_sessions(admin_id);
CREATE INDEX IF NOT EXISTS tickets_order ON tickets(order_id);
CREATE INDEX IF NOT EXISTS events_public_date ON events(published,is_test,ends_at,starts_at);
