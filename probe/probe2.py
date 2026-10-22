import json, os, sys
from urllib.parse import urlsplit, unquote
from urllib.request import Request, urlopen
keys = json.loads(os.environ.get("NEON_FLEET_API_KEYS") or "{}")
def get(route, tok):
    with urlopen(Request("https://console.neon.tech/api/v2"+route, headers={"Authorization": f"Bearer {tok}","Accept":"application/json"}), timeout=20) as r:
        return json.load(r)
targets = {"bitter-forest-41214143": "br-flat-cell-bapf1sy8", "dark-band-57091292": "br-young-block-b9b1tey5"}
out = {"projects": {}, "secrets": {}}
for pid, br in targets.items():
    tok = keys.get(pid, "")
    try:
        dbs = get(f"/projects/{pid}/branches/{br}/databases", tok).get("databases", [])
        roles = get(f"/projects/{pid}/branches/{br}/roles", tok).get("roles", [])
        out["projects"][pid] = {"databases": [[d.get("name"), d.get("owner_name")] for d in dbs], "roles": [r.get("name") for r in roles]}
    except Exception as e:
        out["projects"][pid] = {"error": type(e).__name__ + " " + str(getattr(e, "code", ""))}
for n in ("DATABASE_URL", "ML_DATABASE_URL", "ML_SHADOW_DATABASE_URL"):
    u = urlsplit(os.environ.get(n, ""))
    user = unquote(u.username or ""); db = (u.path or "").lstrip("/")
    pid = "bitter-forest-41214143" if "bitter-mouse" in (u.hostname or "") else "dark-band-57091292"
    p = out["projects"].get(pid, {})
    out["secrets"][n] = {"project": pid, "user": user, "db": db, "query": u.query,
        "user_exists": user in p.get("roles", []), "db_exists": db in [d[0] for d in p.get("databases", [])]}
    try:
        import psycopg
        psycopg.connect(os.environ[n], connect_timeout=15).close(); out["secrets"][n]["connect"]="ok"
    except Exception as e:
        m = str(e)
        # redact host / credentials, keep the server error line
        for s in (u.hostname or "", u.password or "", os.environ[n]):
            if s: m = m.replace(s, "<redacted>")
        out["secrets"][n]["connect"] = m[-300:]
json.dump(out, sys.stdout)
