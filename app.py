"""
Fusion Backend - a lightweight PlayFab-style game backend.
Covers: player auth/ID creation, virtual currencies, player data (cloud save),
inventory/catalog/store, and an admin API for the dashboard.

Deploy target: Render (Flask + SQLite), same pattern as the Discord bot.
"""

import os
import sqlite3
import secrets
import string
import json
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, request, jsonify, g
from flask_cors import CORS

app = Flask(__name__)

# CORS: allow your GitHub Pages admin dashboard + your game (Unity uses UnityWebRequest,
# not a browser, so CORS doesn't apply there, but we still allow it for safety)
CORS(app)

DB_PATH = os.environ.get("DB_PATH", "fusion_backend.db")

# Set this in Render's environment variables. NEVER hardcode this in a public repo.
ADMIN_KEY = os.environ.get("ADMIN_KEY", "change-me-immediately")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS players (
            player_id TEXT PRIMARY KEY,
            device_id TEXT UNIQUE NOT NULL,
            session_token TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT NOT NULL,
            last_login TEXT NOT NULL,
            banned INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS currency_definitions (
            currency_code TEXT PRIMARY KEY,
            currency_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS currencies (
            player_id TEXT NOT NULL,
            currency_code TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (player_id, currency_code),
            FOREIGN KEY (player_id) REFERENCES players(player_id)
        );

        CREATE TABLE IF NOT EXISTS player_data (
            player_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (player_id, key),
            FOREIGN KEY (player_id) REFERENCES players(player_id)
        );

        CREATE TABLE IF NOT EXISTS catalog (
            item_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            currency_code TEXT NOT NULL,
            price INTEGER NOT NULL,
            icon_url TEXT,
            item_class TEXT,
            custom_data TEXT
        );

        CREATE TABLE IF NOT EXISTS inventory (
            instance_id TEXT PRIMARY KEY,
            player_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            custom_data TEXT,
            acquired_at TEXT NOT NULL,
            FOREIGN KEY (player_id) REFERENCES players(player_id),
            FOREIGN KEY (item_id) REFERENCES catalog(item_id)
        );

        CREATE TABLE IF NOT EXISTS player_logins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            logged_in_at TEXT NOT NULL,
            FOREIGN KEY (player_id) REFERENCES players(player_id)
        );

        CREATE TABLE IF NOT EXISTS cloud_scripts (
            script_name TEXT PRIMARY KEY,
            actions_json TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cloud_script_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id TEXT NOT NULL,
            script_name TEXT NOT NULL,
            result_json TEXT,
            success INTEGER NOT NULL,
            executed_at TEXT NOT NULL,
            FOREIGN KEY (player_id) REFERENCES players(player_id)
        );
        """
    )
    conn.commit()

    # Migration: add ban_reason / ban_expires_at to players if upgrading from an older schema
    for column, coltype in [("ban_reason", "TEXT"), ("ban_expires_at", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE players ADD COLUMN {column} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migration: currency grant settings
    for column, coltype in [
        ("initial_amount", "INTEGER NOT NULL DEFAULT 0"),
        ("daily_amount", "INTEGER NOT NULL DEFAULT 0"),
        ("daily_max", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE currency_definitions ADD COLUMN {column} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    try:
        conn.execute("ALTER TABLE currencies ADD COLUMN last_daily_grant TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_player_id():
    # e.g. PLAYER-7F3K9Q
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"PLAYER-{suffix}"


def new_token():
    return secrets.token_hex(24)


def new_instance_id():
    return secrets.token_hex(12)


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------

def is_ban_still_active(db, player_row):
    """Returns True if the player is currently banned. Auto-lifts the ban in the DB
    if it had an expiry time that has already passed."""
    if not player_row["banned"]:
        return False

    expires_at = player_row["ban_expires_at"]
    if not expires_at:
        return True  # permanent ban, no expiry

    try:
        expiry_dt = datetime.fromisoformat(expires_at)
    except ValueError:
        return True

    if datetime.now(timezone.utc) < expiry_dt:
        return True

    # Ban has expired — lift it automatically
    db.execute(
        "UPDATE players SET banned = 0, ban_reason = NULL, ban_expires_at = NULL WHERE player_id = ?",
        (player_row["player_id"],),
    )
    db.commit()
    return False


def require_player(f):
    """Requires a valid Bearer session token, injects player_id into kwargs."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Missing bearer token"}), 401
        token = auth.split(" ", 1)[1]
        db = get_db()
        row = db.execute(
            "SELECT * FROM players WHERE session_token = ?", (token,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Invalid session token"}), 401
        if is_ban_still_active(db, row):
            return jsonify(
                {
                    "error": "This account is banned",
                    "ban_reason": row["ban_reason"],
                    "ban_expires_at": row["ban_expires_at"],
                }
            ), 403
        return f(player=row, *args, **kwargs)

    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-Admin-Key", "")
        if not secrets.compare_digest(key, ADMIN_KEY):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Auth / player creation
# ---------------------------------------------------------------------------
def apply_currency_grants(db, player_id):
    """Grants each currency's starting amount the first time a player sees it, then
    tops them up daily. Mirrors PlayFab's initial deposit + recharge rate model."""
    now = datetime.now(timezone.utc)

    for d in db.execute("SELECT * FROM currency_definitions").fetchall():
        code = d["currency_code"]
        row = db.execute(
            "SELECT amount, last_daily_grant FROM currencies WHERE player_id = ? AND currency_code = ?",
            (player_id, code),
        ).fetchone()

        # First time this player has seen this currency — grant the starting amount.
        if row is None:
            db.execute(
                "INSERT INTO currencies (player_id, currency_code, amount, last_daily_grant) VALUES (?, ?, ?, ?)",
                (player_id, code, d["initial_amount"], now.isoformat()),
            )
            db.commit()
            continue

        daily = d["daily_amount"]
        if not daily or daily <= 0:
            continue

        last = row["last_daily_grant"]
        if not last:
            db.execute(
                "UPDATE currencies SET last_daily_grant = ? WHERE player_id = ? AND currency_code = ?",
                (now.isoformat(), player_id, code),
            )
            db.commit()
            continue

        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            continue

        days = int((now - last_dt).total_seconds() // 86400)
        if days < 1:
            continue

        earned = days * daily
        current = row["amount"]
        cap = d["daily_max"]

        # Cap works like PlayFab's recharge maximum: no daily grant once you're at
        # or above the cap, and a partial top-up if you'd overshoot it.
        if cap is not None:
            earned = 0 if current >= cap else min(earned, cap - current)

        # Advance by whole days only, so leftover hours carry into the next grant
        # instead of silently resetting the clock on every login.
        new_last = (last_dt + timedelta(days=days)).isoformat()
        db.execute(
            "UPDATE currencies SET amount = ?, last_daily_grant = ? WHERE player_id = ? AND currency_code = ?",
            (current + earned, new_last, player_id, code),
        )
        db.commit()

@app.route("/api/auth/login", methods=["POST"])
def login():
    """
    Called by Unity on game start. If the device_id has never been seen,
    a new player ID is created automatically.
    """
    body = request.get_json(force=True, silent=True) or {}
    device_id = body.get("device_id")
    display_name = body.get("display_name")

    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    db = get_db()
    row = db.execute(
        "SELECT * FROM players WHERE device_id = ?", (device_id,)
    ).fetchone()

    is_new = False
    if row is None:
        player_id = new_player_id()
        token = new_token()
        db.execute(
            """INSERT INTO players
               (player_id, device_id, session_token, display_name, created_at, last_login, banned)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (player_id, device_id, token, display_name, now_iso(), now_iso()),
        )
        db.commit()
        is_new = True
        row = db.execute(
            "SELECT * FROM players WHERE player_id = ?", (player_id,)
        ).fetchone()
    else:
        # refresh session token + last_login on every login
        token = new_token()
        db.execute(
            "UPDATE players SET session_token = ?, last_login = ? WHERE player_id = ?",
            (token, now_iso(), row["player_id"]),
        )
        db.commit()

    db.execute(
        "INSERT INTO player_logins (player_id, logged_in_at) VALUES (?, ?)",
        (row["player_id"], now_iso()),
    )
    db.commit()

    apply_currency_grants(db, row["player_id"])

    banned = is_ban_still_active(db, row)
    if banned:
        row = db.execute("SELECT * FROM players WHERE player_id = ?", (row["player_id"],)).fetchone()

    return jsonify(
        {
            "player_id": row["player_id"],
            "session_token": token,
            "display_name": row["display_name"],
            "is_new_player": is_new,
            "banned": banned,
            "ban_reason": row["ban_reason"] if banned else None,
            "ban_expires_at": row["ban_expires_at"] if banned else None,
        }
    )

@app.route("/api/profile/display-name", methods=["POST"])
@require_player
def set_display_name(player):
    """Sets the logged-in player's display name (shown in the admin dashboard)."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("display_name") or "").strip()

    if not name:
        return jsonify({"error": "display_name is required"}), 400
    if len(name) > 12:
        return jsonify({"error": "display_name must be 12 characters or fewer"}), 400

    db = get_db()
    db.execute(
        "UPDATE players SET display_name = ? WHERE player_id = ?",
        (name, player["player_id"]),
    )
    db.commit()
    return jsonify({"success": True, "display_name": name})

# ---------------------------------------------------------------------------
# Player data / cloud save
# ---------------------------------------------------------------------------

@app.route("/api/data", methods=["GET"])
@require_player
def get_data(player):
    db = get_db()
    rows = db.execute(
        "SELECT key, value FROM player_data WHERE player_id = ?", (player["player_id"],)
    ).fetchall()
    data = {r["key"]: json.loads(r["value"]) for r in rows}
    return jsonify({"data": data})


@app.route("/api/data", methods=["POST"])
@require_player
def set_data(player):
    body = request.get_json(force=True, silent=True) or {}
    data = body.get("data")
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be {'data': {key: value, ...}}"}), 400

    db = get_db()
    for key, value in data.items():
        db.execute(
            """INSERT INTO player_data (player_id, key, value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(player_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (player["player_id"], key, json.dumps(value), now_iso()),
        )
    db.commit()
    return jsonify({"success": True, "keys_updated": list(data.keys())})


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

@app.route("/api/currency/redeem-iap", methods=["POST"])
@require_player
def redeem_iap(player):
    """Grants currency for a real-money Meta Store purchase.

    The client sends only a SKU — never an amount — so a modified build can't
    ask for an arbitrary number of coins. How much each SKU is worth is defined
    here on the server via a cloud script named 'iap_<sku>'.
    """
    body = request.get_json(force=True, silent=True) or {}
    sku = (body.get("sku") or "").strip()
    if not sku:
        return jsonify({"error": "sku is required"}), 400

    # Reuse the cloud script system: create a script named e.g. "iap_cube-001"
    # in the dashboard with a grant_currency action for that pack's amount.
    script_name = f"iap_{sku}"
    db = get_db()
    success, results = run_cloud_script(db, player["player_id"], script_name)

    if not success:
        return jsonify({"error": f"No payout configured for SKU '{sku}'", "results": results}), 400

    return jsonify({"success": True, "sku": sku, "results": results})

@app.route("/api/currency", methods=["GET"])
@require_player
def get_currency(player):
    db = get_db()
    rows = db.execute(
        "SELECT currency_code, amount FROM currencies WHERE player_id = ?",
        (player["player_id"],),
    ).fetchall()
    return jsonify({"currencies": {r["currency_code"]: r["amount"] for r in rows}})


def currency_exists(db, code):
    row = db.execute(
        "SELECT 1 FROM currency_definitions WHERE currency_code = ?", (code,)
    ).fetchone()
    return row is not None


def _adjust_currency(db, player_id, code, delta):
    row = db.execute(
        "SELECT amount FROM currencies WHERE player_id = ? AND currency_code = ?",
        (player_id, code),
    ).fetchone()
    current = row["amount"] if row else 0
    new_amount = current + delta
    if new_amount < 0:
        return None  # insufficient funds
    if row:
        db.execute(
            "UPDATE currencies SET amount = ? WHERE player_id = ? AND currency_code = ?",
            (new_amount, player_id, code),
        )
    else:
        db.execute(
            "INSERT INTO currencies (player_id, currency_code, amount) VALUES (?, ?, ?)",
            (player_id, code, new_amount),
        )
    db.commit()
    return new_amount


@app.route("/api/currency/add", methods=["POST"])
@require_player
def add_currency(player):
    body = request.get_json(force=True, silent=True) or {}
    code = body.get("currency_code")
    amount = body.get("amount")
    if not code or not isinstance(amount, int) or amount <= 0:
        return jsonify({"error": "currency_code and positive integer amount required"}), 400
    db = get_db()
    if not currency_exists(db, code):
        return jsonify({"error": f"Currency '{code}' has not been created yet. Create it in the admin dashboard first."}), 400
    new_amount = _adjust_currency(db, player["player_id"], code, amount)
    return jsonify({"currency_code": code, "amount": new_amount})


@app.route("/api/currency/subtract", methods=["POST"])
@require_player
def subtract_currency(player):
    body = request.get_json(force=True, silent=True) or {}
    code = body.get("currency_code")
    amount = body.get("amount")
    if not code or not isinstance(amount, int) or amount <= 0:
        return jsonify({"error": "currency_code and positive integer amount required"}), 400
    db = get_db()
    if not currency_exists(db, code):
        return jsonify({"error": f"Currency '{code}' has not been created yet. Create it in the admin dashboard first."}), 400
    new_amount = _adjust_currency(db, player["player_id"], code, -amount)
    if new_amount is None:
        return jsonify({"error": "Insufficient funds"}), 400
    return jsonify({"currency_code": code, "amount": new_amount})


# ---------------------------------------------------------------------------
# Currency definitions (must be created before a currency code can be used)
# ---------------------------------------------------------------------------

@app.route("/api/currencies", methods=["GET"])
def get_currency_definitions():
    """Public — lets Unity (and the dashboard) fetch which currencies exist."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM currency_definitions ORDER BY currency_name"
    ).fetchall()
    return jsonify(
        {
            "currencies": [
                {
                    "code": r["currency_code"],
                    "name": r["currency_name"],
                    "initial_amount": r["initial_amount"],
                    "daily_amount": r["daily_amount"],
                    "daily_max": r["daily_max"],
                }
                for r in rows
            ]
        }
    )


@app.route("/api/admin/currencies", methods=["POST"])
@require_admin
def admin_create_currency():
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip().upper()
    name = (body.get("name") or "").strip()
    if not code or not name:
        return jsonify({"error": "code and name are required"}), 400

    try:
        initial_amount = int(body.get("initial_amount") or 0)
        daily_amount = int(body.get("daily_amount") or 0)
        daily_max = body.get("daily_max")
        daily_max = int(daily_max) if daily_max not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "initial_amount, daily_amount and daily_max must be whole numbers"}), 400

    if initial_amount < 0 or daily_amount < 0 or (daily_max is not None and daily_max < 0):
        return jsonify({"error": "amounts cannot be negative"}), 400

    db = get_db()
    if db.execute("SELECT 1 FROM currency_definitions WHERE currency_code = ?", (code,)).fetchone():
        return jsonify({"error": f"Currency '{code}' already exists"}), 400

    db.execute(
        """INSERT INTO currency_definitions
           (currency_code, currency_name, created_at, initial_amount, daily_amount, daily_max)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (code, name, now_iso(), initial_amount, daily_amount, daily_max),
    )
    db.commit()
    return jsonify({"success": True, "code": code, "name": name})


@app.route("/api/admin/currencies/<code>", methods=["DELETE"])
@require_admin
def admin_delete_currency(code):
    db = get_db()
    db.execute("DELETE FROM currency_definitions WHERE currency_code = ?", (code,))
    db.commit()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Catalog / Store / Inventory
# ---------------------------------------------------------------------------

@app.route("/api/catalog", methods=["GET"])
def get_catalog():
    db = get_db()
    rows = db.execute("SELECT * FROM catalog").fetchall()
    items = [
        {
            "item_id": r["item_id"],
            "name": r["name"],
            "description": r["description"],
            "currency_code": r["currency_code"],
            "price": r["price"],
            "icon_url": r["icon_url"],
            "item_class": r["item_class"],
            "custom_data": json.loads(r["custom_data"]) if r["custom_data"] else None,
        }
        for r in rows
    ]
    return jsonify({"catalog": items})


@app.route("/api/inventory", methods=["GET"])
@require_player
def get_inventory(player):
    db = get_db()
    rows = db.execute(
        """SELECT inventory.*, catalog.name, catalog.item_class
           FROM inventory JOIN catalog ON inventory.item_id = catalog.item_id
           WHERE inventory.player_id = ?""",
        (player["player_id"],),
    ).fetchall()
    items = [
        {
            "instance_id": r["instance_id"],
            "item_id": r["item_id"],
            "name": r["name"],
            "item_class": r["item_class"],
            "quantity": r["quantity"],
            "custom_data": json.loads(r["custom_data"]) if r["custom_data"] else None,
            "acquired_at": r["acquired_at"],
        }
        for r in rows
    ]
    return jsonify({"inventory": items})


@app.route("/api/store/purchase", methods=["POST"])
@require_player
def purchase(player):
    body = request.get_json(force=True, silent=True) or {}
    item_id = body.get("item_id")
    quantity = body.get("quantity", 1)
    if not item_id or not isinstance(quantity, int) or quantity <= 0:
        return jsonify({"error": "item_id and positive integer quantity required"}), 400

    db = get_db()
    item = db.execute("SELECT * FROM catalog WHERE item_id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Item not found in catalog"}), 404

    total_cost = item["price"] * quantity
    new_balance = _adjust_currency(db, player["player_id"], item["currency_code"], -total_cost)
    if new_balance is None:
        return jsonify({"error": "Insufficient funds"}), 400

    instance_id = new_instance_id()
    db.execute(
        """INSERT INTO inventory (instance_id, player_id, item_id, quantity, custom_data, acquired_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (instance_id, player["player_id"], item_id, quantity, None, now_iso()),
    )
    db.commit()
    return jsonify(
        {
            "success": True,
            "instance_id": instance_id,
            "item_id": item_id,
            "quantity": quantity,
            "new_currency_balance": {item["currency_code"]: new_balance},
        }
    )


# ---------------------------------------------------------------------------
# Cloud Script
#
# Real PlayFab CloudScript runs arbitrary server-side JavaScript. Running
# arbitrary code triggered by a game client is a real security risk (a
# modified client could execute anything on your server), so this is a safer
# equivalent: named, admin-defined bundles of fixed actions (grant currency,
# grant an item, subtract currency, set a data key). Unity triggers a script
# by name only — it can never choose what the script does, just that it runs.
# ---------------------------------------------------------------------------

VALID_ACTION_TYPES = {"grant_currency", "subtract_currency", "grant_item", "set_data"}


def run_cloud_script(db, player_id, script_name):
    """Executes a named script's actions against a player. Returns (success, results)."""
    script = db.execute(
        "SELECT * FROM cloud_scripts WHERE script_name = ?", (script_name,)
    ).fetchone()
    if not script:
        return False, [{"error": f"Cloud script '{script_name}' not found"}]

    try:
        actions = json.loads(script["actions_json"])
    except (TypeError, ValueError):
        return False, [{"error": "Script has malformed actions"}]

    results = []
    overall_success = True

    for action in actions:
        a_type = action.get("type")
        if a_type == "grant_currency":
            code = action.get("currency_code")
            amount = int(action.get("amount", 0))
            if not currency_exists(db, code):
                results.append({"type": a_type, "error": f"Currency '{code}' does not exist"})
                overall_success = False
                continue
            new_amount = _adjust_currency(db, player_id, code, amount)
            results.append({"type": a_type, "currency_code": code, "new_amount": new_amount})

        elif a_type == "subtract_currency":
            code = action.get("currency_code")
            amount = int(action.get("amount", 0))
            if not currency_exists(db, code):
                results.append({"type": a_type, "error": f"Currency '{code}' does not exist"})
                overall_success = False
                continue
            new_amount = _adjust_currency(db, player_id, code, -amount)
            if new_amount is None:
                results.append({"type": a_type, "error": "Insufficient funds"})
                overall_success = False
            else:
                results.append({"type": a_type, "currency_code": code, "new_amount": new_amount})

        elif a_type == "grant_item":
            item_id = action.get("item_id")
            quantity = int(action.get("quantity", 1))
            item = db.execute("SELECT * FROM catalog WHERE item_id = ?", (item_id,)).fetchone()
            if not item:
                results.append({"type": a_type, "error": f"Item '{item_id}' not found in catalog"})
                overall_success = False
                continue
            instance_id = new_instance_id()
            db.execute(
                """INSERT INTO inventory (instance_id, player_id, item_id, quantity, custom_data, acquired_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (instance_id, player_id, item_id, quantity, None, now_iso()),
            )
            db.commit()
            results.append({"type": a_type, "item_id": item_id, "quantity": quantity, "instance_id": instance_id})

        elif a_type == "set_data":
            key = action.get("key")
            value = action.get("value")
            db.execute(
                """INSERT INTO player_data (player_id, key, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(player_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (player_id, key, json.dumps(value), now_iso()),
            )
            db.commit()
            results.append({"type": a_type, "key": key, "value": value})

        else:
            results.append({"type": a_type, "error": "Unknown action type"})
            overall_success = False

    db.execute(
        "INSERT INTO cloud_script_log (player_id, script_name, result_json, success, executed_at) VALUES (?, ?, ?, ?, ?)",
        (player_id, script_name, json.dumps(results), 1 if overall_success else 0, now_iso()),
    )
    db.commit()

    return overall_success, results


@app.route("/api/cloudscript/execute", methods=["POST"])
@require_player
def execute_cloud_script(player):
    """Called from Unity — runs a named script by name only. The client never
    supplies what the script does, only which one to run."""
    body = request.get_json(force=True, silent=True) or {}
    script_name = body.get("script_name")
    if not script_name:
        return jsonify({"error": "script_name is required"}), 400

    db = get_db()
    success, results = run_cloud_script(db, player["player_id"], script_name)
    return jsonify({"success": success, "script_name": script_name, "results": results})


@app.route("/api/admin/cloudscript", methods=["GET"])
@require_admin
def admin_list_cloud_scripts():
    db = get_db()
    rows = db.execute("SELECT * FROM cloud_scripts ORDER BY script_name").fetchall()
    scripts = [
        {
            "script_name": r["script_name"],
            "description": r["description"],
            "actions": json.loads(r["actions_json"]),
        }
        for r in rows
    ]
    return jsonify({"scripts": scripts})


@app.route("/api/admin/cloudscript", methods=["POST"])
@require_admin
def admin_upsert_cloud_script():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("script_name") or "").strip()
    actions = body.get("actions")
    description = body.get("description")

    if not name or not isinstance(actions, list) or len(actions) == 0:
        return jsonify({"error": "script_name and a non-empty actions list are required"}), 400

    for action in actions:
        if action.get("type") not in VALID_ACTION_TYPES:
            return jsonify({"error": f"Invalid action type: {action.get('type')}. Must be one of {sorted(VALID_ACTION_TYPES)}"}), 400

    db = get_db()
    db.execute(
        """INSERT INTO cloud_scripts (script_name, actions_json, description, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(script_name) DO UPDATE SET actions_json=excluded.actions_json, description=excluded.description""",
        (name, json.dumps(actions), description, now_iso()),
    )
    db.commit()
    return jsonify({"success": True, "script_name": name})


@app.route("/api/admin/cloudscript/<script_name>", methods=["DELETE"])
@require_admin
def admin_delete_cloud_script(script_name):
    db = get_db()
    db.execute("DELETE FROM cloud_scripts WHERE script_name = ?", (script_name,))
    db.commit()
    return jsonify({"success": True})


@app.route("/api/admin/cloudscript/<script_name>/execute/<player_id>", methods=["POST"])
@require_admin
def admin_execute_cloud_script(script_name, player_id):
    """Lets the dashboard manually test-run a script against a specific player."""
    db = get_db()
    player = db.execute("SELECT 1 FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        return jsonify({"error": "Player not found"}), 404
    success, results = run_cloud_script(db, player_id, script_name)
    return jsonify({"success": success, "script_name": script_name, "results": results})


@app.route("/api/admin/players/<player_id>/cloudscript-log", methods=["GET"])
@require_admin
def admin_get_cloudscript_log(player_id):
    db = get_db()
    rows = db.execute(
        "SELECT script_name, result_json, success, executed_at FROM cloud_script_log WHERE player_id = ? ORDER BY executed_at DESC LIMIT 50",
        (player_id,),
    ).fetchall()
    return jsonify(
        {
            "log": [
                {
                    "script_name": r["script_name"],
                    "results": json.loads(r["result_json"]) if r["result_json"] else [],
                    "success": bool(r["success"]),
                    "executed_at": r["executed_at"],
                }
                for r in rows
            ]
        }
    )


# ---------------------------------------------------------------------------
# Admin API (used by the GitHub Pages dashboard)
# ---------------------------------------------------------------------------

@app.route("/api/admin/players", methods=["GET"])
@require_admin
def admin_list_players():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM players ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        still_banned = is_ban_still_active(db, r)
        result.append(
            {
                "player_id": r["player_id"],
                "device_id": r["device_id"],
                "display_name": r["display_name"],
                "created_at": r["created_at"],
                "last_login": r["last_login"],
                "banned": still_banned,
            }
        )
    return jsonify({"players": result})


@app.route("/api/admin/players/<player_id>", methods=["GET"])
@require_admin
def admin_get_player(player_id):
    db = get_db()
    player = db.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        return jsonify({"error": "Player not found"}), 404

    # Auto-lift the ban if it already expired, then re-fetch the fresh row
    is_ban_still_active(db, player)
    player = db.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()

    currencies = db.execute(
        "SELECT currency_code, amount FROM currencies WHERE player_id = ?", (player_id,)
    ).fetchall()
    data_rows = db.execute(
        "SELECT key, value FROM player_data WHERE player_id = ?", (player_id,)
    ).fetchall()
    inventory = db.execute(
        """SELECT inventory.*, catalog.name FROM inventory
           JOIN catalog ON inventory.item_id = catalog.item_id
           WHERE inventory.player_id = ?""",
        (player_id,),
    ).fetchall()

    return jsonify(
        {
            "player": {
                "player_id": player["player_id"],
                "device_id": player["device_id"],
                "display_name": player["display_name"],
                "created_at": player["created_at"],
                "last_login": player["last_login"],
                "banned": bool(player["banned"]),
                "ban_reason": player["ban_reason"],
                "ban_expires_at": player["ban_expires_at"],
            },
            "currencies": {r["currency_code"]: r["amount"] for r in currencies},
            "data": {r["key"]: json.loads(r["value"]) for r in data_rows},
            "inventory": [
                {
                    "instance_id": r["instance_id"],
                    "item_id": r["item_id"],
                    "name": r["name"],
                    "quantity": r["quantity"],
                    "acquired_at": r["acquired_at"],
                }
                for r in inventory
            ],
        }
    )


@app.route("/api/admin/players/<player_id>", methods=["DELETE"])
@require_admin
def admin_delete_player(player_id):
    db = get_db()
    player = db.execute("SELECT 1 FROM players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        return jsonify({"error": "Player not found"}), 404
    db.execute("DELETE FROM cloud_script_log WHERE player_id = ?", (player_id,))
    db.execute("DELETE FROM player_logins WHERE player_id = ?", (player_id,))
    db.execute("DELETE FROM inventory WHERE player_id = ?", (player_id,))
    db.execute("DELETE FROM player_data WHERE player_id = ?", (player_id,))
    db.execute("DELETE FROM currencies WHERE player_id = ?", (player_id,))
    db.execute("DELETE FROM players WHERE player_id = ?", (player_id,))
    db.commit()
    return jsonify({"success": True, "deleted": player_id})


@app.route("/api/admin/players/<player_id>/inventory/<instance_id>", methods=["DELETE"])
@require_admin
def admin_revoke_item(player_id, instance_id):
    """Revoke (delete) a specific inventory item instance from a player."""
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM inventory WHERE instance_id = ? AND player_id = ?", (instance_id, player_id)
    ).fetchone()
    if not row:
        return jsonify({"error": "Inventory item not found for this player"}), 404
    db.execute("DELETE FROM inventory WHERE instance_id = ?", (instance_id,))
    db.commit()
    return jsonify({"success": True, "revoked": instance_id})


@app.route("/api/admin/players/<player_id>/currency", methods=["POST"])
@require_admin
def admin_set_currency(player_id):
    body = request.get_json(force=True, silent=True) or {}
    code = body.get("currency_code")
    amount = body.get("amount")  # sets absolute amount
    if not code or not isinstance(amount, int) or amount < 0:
        return jsonify({"error": "currency_code and non-negative integer amount required"}), 400
    db = get_db()
    if not currency_exists(db, code):
        return jsonify({"error": f"Currency '{code}' has not been created yet. Create it in the admin dashboard first."}), 400
    db.execute(
        """INSERT INTO currencies (player_id, currency_code, amount) VALUES (?, ?, ?)
           ON CONFLICT(player_id, currency_code) DO UPDATE SET amount = excluded.amount""",
        (player_id, code, amount),
    )
    db.commit()
    return jsonify({"success": True, "currency_code": code, "amount": amount})


@app.route("/api/admin/players/<player_id>/inventory/grant", methods=["POST"])
@require_admin
def admin_grant_item(player_id):
    body = request.get_json(force=True, silent=True) or {}
    item_id = body.get("item_id")
    quantity = body.get("quantity", 1)
    if not item_id:
        return jsonify({"error": "item_id required"}), 400
    db = get_db()
    item = db.execute("SELECT * FROM catalog WHERE item_id = ?", (item_id,)).fetchone()
    if not item:
        return jsonify({"error": "Item not found in catalog"}), 404
    instance_id = new_instance_id()
    db.execute(
        """INSERT INTO inventory (instance_id, player_id, item_id, quantity, custom_data, acquired_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (instance_id, player_id, item_id, quantity, None, now_iso()),
    )
    db.commit()
    return jsonify({"success": True, "instance_id": instance_id})


@app.route("/api/admin/players/<player_id>/ban", methods=["POST"])
@require_admin
def admin_ban_player(player_id):
    body = request.get_json(force=True, silent=True) or {}
    banned = 1 if body.get("banned", True) else 0
    reason = body.get("reason")
    duration_minutes = body.get("duration_minutes")  # omit/null = permanent ban

    expires_at = None
    if banned and duration_minutes:
        try:
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=float(duration_minutes))).isoformat()
        except (TypeError, ValueError):
            return jsonify({"error": "duration_minutes must be a number"}), 400

    db = get_db()
    db.execute(
        "UPDATE players SET banned = ?, ban_reason = ?, ban_expires_at = ? WHERE player_id = ?",
        (banned, reason if banned else None, expires_at if banned else None, player_id),
    )
    db.commit()
    return jsonify({"success": True, "banned": bool(banned), "reason": reason, "ban_expires_at": expires_at})


@app.route("/api/admin/players/<player_id>/logins", methods=["GET"])
@require_admin
def admin_get_logins(player_id):
    db = get_db()
    rows = db.execute(
        "SELECT logged_in_at FROM player_logins WHERE player_id = ? ORDER BY logged_in_at DESC LIMIT 50",
        (player_id,),
    ).fetchall()
    return jsonify({"logins": [r["logged_in_at"] for r in rows]})


@app.route("/api/admin/catalog", methods=["POST"])
@require_admin
def admin_upsert_catalog_item():
    body = request.get_json(force=True, silent=True) or {}
    required = ["item_id", "name", "currency_code", "price"]
    if not all(k in body for k in required):
        return jsonify({"error": f"Required fields: {required}"}), 400
    db = get_db()
    if not currency_exists(db, body["currency_code"]):
        return jsonify({"error": f"Currency '{body['currency_code']}' has not been created yet. Create it first."}), 400
    db.execute(
        """INSERT INTO catalog (item_id, name, description, currency_code, price, icon_url, item_class, custom_data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
             name=excluded.name, description=excluded.description, currency_code=excluded.currency_code,
             price=excluded.price, icon_url=excluded.icon_url, item_class=excluded.item_class, custom_data=excluded.custom_data""",
        (
            body["item_id"],
            body["name"],
            body.get("description"),
            body["currency_code"],
            body["price"],
            body.get("icon_url"),
            body.get("item_class"),
            json.dumps(body["custom_data"]) if body.get("custom_data") else None,
        ),
    )
    db.commit()
    return jsonify({"success": True, "item_id": body["item_id"]})


@app.route("/api/admin/catalog/<item_id>", methods=["DELETE"])
@require_admin
def admin_delete_catalog_item(item_id):
    db = get_db()
    db.execute("DELETE FROM catalog WHERE item_id = ?", (item_id,))
    db.commit()
    return jsonify({"success": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": now_iso()})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    # also run init_db when imported by gunicorn on Render
    init_db()
