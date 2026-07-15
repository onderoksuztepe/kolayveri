import csv
from datetime import datetime, timezone
from pymongo import MongoClient

CSV_PATH = "/opt/kolayveri_ami/ami_sayaclar.csv"
DB_NAME = "amimavialp"

client = MongoClient("mongodb://127.0.0.1:27017")
db = client[DB_NAME]
meters = db.meters

def read_csv(path):
    for enc in ["utf-8-sig", "utf-8", "cp1254", "latin1"]:
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
            return rows
        except UnicodeDecodeError:
            continue
    raise RuntimeError("CSV encoding okunamadı.")

rows = read_csv(CSV_PATH)

updated = 0
not_found = []

for row in rows:
    meter_serial = str(row.get("AMİ Sayaç no") or row.get("AMI Sayaç no") or "").strip()
    sort_raw = str(row.get("Sıra No") or row.get("Sira No") or "").strip()

    if not meter_serial or not sort_raw:
        continue

    try:
        sort_order = int(float(sort_raw.replace(",", ".")))
    except Exception:
        print(f"Sıra No okunamadı: {meter_serial} -> {sort_raw}")
        continue

    result = meters.update_one(
        {
            "meter_serial": meter_serial,
            "ami_master_updated_at": {"$exists": True}
        },
        {
            "$set": {
                "sort_order": sort_order,
                "display_order": sort_order,
                "sort_order_updated_at": datetime.now(timezone.utc),
            }
        }
    )

    if result.matched_count:
        updated += 1
    else:
        not_found.append(meter_serial)

print(f"Güncellenen sayaç: {updated}")
print(f"Bulunamayan sayaç: {len(not_found)}")

if not_found:
    print("Bulunamayanlar:")
    for x in not_found:
        print(" -", x)
