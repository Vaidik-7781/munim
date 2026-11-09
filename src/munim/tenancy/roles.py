"""Who may do what. Deny by default; every command/callback maps to exactly one action."""
from __future__ import annotations

OWNER, STAFF, CUSTOMER = "owner", "staff", "customer"

# actions
ENTRY, QUERY, ADMIN, MONEY_ADMIN, COMMUNICATE, UNDO, PORTAL = (
    "entry", "query", "admin", "money_admin", "communicate", "undo", "portal")

PERMS: dict[str, frozenset[str]] = {
    OWNER: frozenset({ENTRY, QUERY, ADMIN, MONEY_ADMIN, COMMUNICATE, UNDO}),
    # a munshi/helper can record and look things up; cannot undo, set limits, grant consent, message customers, or export
    STAFF: frozenset({ENTRY, QUERY}),
    CUSTOMER: frozenset({PORTAL}),
}

COMMAND_ACTION = {
    "start": None, "help": None,
    "hisaab": QUERY, "ask": QUERY, "risk": QUERY, "forecast": QUERY, "anomaly": QUERY, "briefing": QUERY,
    "add": ENTRY, "dena": ENTRY,
    "plan": COMMUNICATE, "statement": COMMUNICATE, "from": COMMUNICATE, "invite": COMMUNICATE, "paylink": COMMUNICATE,
    "consent": MONEY_ADMIN, "limit": MONEY_ADMIN, "lang": ADMIN, "voice": ADMIN, "voices": QUERY,
    "staff": ADMIN, "export": ADMIN, "stats": ADMIN, "scan": ADMIN, "import": ADMIN, "setvpa": ADMIN,
    "stock": QUERY, "count": ENTRY, "price": ADMIN, "ondc": ADMIN, "pincode": ADMIN, "mandate": MONEY_ADMIN, "insure": ADMIN, "pool": ADMIN,
    "passport": QUERY, "loan": ADMIN, "policy": ADMIN, "recon": MONEY_ADMIN, "edge": ADMIN, "smsdev": ADMIN, "invoice": ADMIN, "aa": ADMIN,
    "dispute": QUERY, "score": QUERY, "shop": QUERY, "lena": QUERY, "break": None, "newshop": None,
}


def can(role: str | None, action: str | None) -> bool:
    if action is None:
        return True
    return role is not None and action in PERMS.get(role, frozenset())
