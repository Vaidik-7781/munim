"""Per-shop auxiliary store (<shop>_aux.db): everything that is NOT the hash-chained money ledger.

Inventory, ONDC outbox, mandates, premium accruals, supplier dues and small key/value settings live here. Money-affecting
facts that matter for audit (mandate lifecycle, passport shares, loan consents, sweeps) are ALSO appended to the main
hash chain as non-money entries, so tampering with this file cannot hide them. Amounts are integer paise; stock is integer
base units (grams / millilitres / pieces) — no floats anywhere near money or counts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sku(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, dim TEXT NOT NULL,
  price_paise INTEGER, price_per INTEGER NOT NULL DEFAULT 1, mrp_paise INTEGER, stock INTEGER NOT NULL DEFAULT 0,
  reorder INTEGER, manufacturer TEXT, hsn TEXT, gst_pct INTEGER, verified_ts REAL, ondc INTEGER NOT NULL DEFAULT 1,
  created REAL);
CREATE TABLE IF NOT EXISTS stock_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, sku_id INTEGER NOT NULL, delta INTEGER NOT NULL,
  kind TEXT NOT NULL, src TEXT, reverses INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS idx_se_sku ON stock_events(sku_id, ts);
CREATE TABLE IF NOT EXISTS ondc_outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', ref TEXT);
CREATE TABLE IF NOT EXISTS payable_due(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, supplier TEXT NOT NULL, amount_paise INTEGER NOT NULL,
  due_iso TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', invoice_hash TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS mandates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, status TEXT NOT NULL,
  cap_pct INTEGER NOT NULL, per_sweep_paise INTEGER NOT NULL, total_cap_paise INTEGER NOT NULL,
  swept_paise INTEGER NOT NULL DEFAULT 0, sources TEXT NOT NULL, created REAL, accepted_ts REAL, expires REAL,
  rail_ref TEXT, terms_hash TEXT);
CREATE TABLE IF NOT EXISTS sweeps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, mandate_id INTEGER NOT NULL, ts REAL NOT NULL, credit_paise INTEGER NOT NULL,
  sweep_paise INTEGER NOT NULL, rail_ref TEXT UNIQUE, ledger_seq INTEGER);
CREATE TABLE IF NOT EXISTS policies(
  id INTEGER PRIMARY KEY AUTOINCREMENT, product TEXT NOT NULL, tier TEXT NOT NULL, sum_insured_paise INTEGER NOT NULL,
  annual_premium_paise INTEGER NOT NULL, status TEXT NOT NULL, insurer_ref TEXT, created REAL, start_ts REAL,
  accrued_paise INTEGER NOT NULL DEFAULT 0, paid_paise INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS accruals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, policy_id INTEGER NOT NULL, ref_hash TEXT UNIQUE, paise INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, policy_id INTEGER NOT NULL, summary TEXT, est_loss_paise INTEGER,
  status TEXT NOT NULL DEFAULT 'draft', insurer_ref TEXT);
"""


class Aux:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def get(self, k: str, default: Any = None) -> Any:
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def put(self, k: str, v: Any) -> None:
        self.db.execute("INSERT INTO kv(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def close(self) -> None:
        self.db.close()
