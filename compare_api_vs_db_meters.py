import psycopg2
import requests

DB_CONFIG = {
    "host": "127.0.0.1",
    "dbname": "kolayveri_db",
    "user": "kolayveri_user",
    "password": "Kv2026ChangeMe123",
    "port": 5432,
}

API_URL = "http://127.0.0.1:8000/api/meters/last-vs-previous"

def normalize_serial(serial):
    if serial is None:
        return None
    s = str(serial).strip()
    upper = s.upper()
    for prefix in ("MSY", "AEL"):
        if upper.startswith(prefix):
            s = s[len(prefix):]
            upper = s.upper()
            break
    if s.isdigit():
        s = s.lstrip("0") or "0"
    return s

api_rows = requests.get(API_URL, timeout=120).json()

api_map = {}
for item in api_rows:
    serial = normalize_serial(item.get("meter_serial"))
    api_map[serial] = item

conn = psycopg2.connect(**DB_CONFIG)

with conn.cursor() as cur:
    cur.execute("""
        SELECT meter_serial, meter_name, sort_order
        FROM meters
        WHERE status = 'active'
        ORDER BY sort_order NULLS LAST
    """)
    db_rows = cur.fetchall()

conn.close()

db_map = {
    normalize_serial(r[0]): {
        "original_serial": r[0],
        "name": r[1],
        "sort_order": r[2]
    }
    for r in db_rows
}

api_serials = set(api_map.keys())
db_serials = set(db_map.keys())

only_api = sorted(api_serials - db_serials)
only_db = sorted(db_serials - api_serials)

print("API toplam:", len(api_serials))
print("DB toplam:", len(db_serials))
print("Eşleşen:", len(api_serials & db_serials))
print("Sadece API'de olan:", len(only_api))
print("Sadece DB'de olan:", len(only_db))

print("\n--- SADECE API'DE OLAN ---")
for s in only_api[:100]:
    item = api_map[s]
    print(s, "|", item.get("meter_serial"), "|", item.get("name"), "|", item.get("group_id"))

print("\n--- SADECE DB'DE OLAN ---")
for s in only_db[:100]:
    item = db_map[s]
    print(s, "|", item.get("original_serial"), "|", item.get("sort_order"), "|", item.get("name"))
