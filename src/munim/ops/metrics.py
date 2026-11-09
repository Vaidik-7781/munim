"""Prometheus text exposition. No names, amounts or chat ids: only counts, and shops identified by a short hash."""
from __future__ import annotations

import hashlib


def render(registry) -> str:
    q = registry.quota.snapshot()
    roles: dict[str, int] = {}
    for r in registry._db.execute("SELECT role, COUNT(*) c FROM members GROUP BY role").fetchall():
        roles[r["role"]] = r["c"]
    L = ["# TYPE munim_shops gauge", f"munim_shops {registry.shop_count()}", "# TYPE munim_members gauge"]
    L += [f'munim_members{{role="{k}"}} {v}' for k, v in sorted(roles.items())]
    L += ["# TYPE munim_gnani_calls_today gauge", f"munim_gnani_calls_today {q['global_used']}",
          "# TYPE munim_gnani_calls_cap gauge", f"munim_gnani_calls_cap {q['global_cap']}",
          "# TYPE munim_shop_gnani_calls_today gauge"]
    for sid, n in sorted(q["shops"].items()):
        L.append(f'munim_shop_gnani_calls_today{{shop="{hashlib.sha256(sid.encode()).hexdigest()[:8]}"}} {n}')
    return "\n".join(L) + "\n"
