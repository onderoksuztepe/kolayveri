import csv
import argparse
from datetime import datetime, timezone
from pymongo import MongoClient

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "amimavialp"
DEFAULT_CSV_PATH = "/opt/kolayveri_ami/first_readings.csv"

parser = argparse.ArgumentParser()
parser.add_argument("--csv", default=DEFAULT_CSV_PATH)
parser.add_argument("--apply", action="store_true")
parser.add_argument("--report", default="/opt/kolayveri_ami/first_readings_update_report.csv")
args = parser.parse_args()

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
meters = db["meters"]

def parse_number(value):
    """
    Kabul edilen örnekler:
    8450.61
    8,450.61
    8450,61
    8.450,61
    """
    s = str(value or "").strip().replace('"', "").replace(" ", "")

    if not s:
        raise ValueError("boş değer")

    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        # Son görülen ayraç decimal kabul edilir.
        if s.rfind(".") > s.rfind(","):
            # 8,450.61 -> 8450.61
            s = s.replace(",", "")
        else:
            # 8.450,61 -> 8450.61
            s = s.replace(".", "").replace(",", ".")
    elif has_comma:
        # 8450,61 -> 8450.61
        s = s.replace(",", ".")
    else:
        # 8450.61
        pass

    return float(s)

updated = 0
not_found = []
skipped = []
rows_report = []

with open(args.csv, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)

    required = {"meter_serial", "first_value"}
    missing_cols = required - set(reader.fieldnames or [])
    if missing_cols:
        raise SystemExit(f"Eksik kolon var: {missing_cols}. Gerekli kolonlar: meter_serial, first_value")

    for row in reader:
        meter_serial = str(row.get("meter_serial") or "").strip()
        raw_value = str(row.get("first_value") or "").strip()

        if not meter_serial or not raw_value:
            skipped.append((meter_serial, raw_value, "boş meter_serial veya first_value"))
            continue

        try:
            first_value = parse_number(raw_value)
        except Exception as e:
            skipped.append((meter_serial, raw_value, f"first_value sayıya çevrilemedi: {e}"))
            continue

        doc = meters.find_one(
            {
                "meter_serial": meter_serial,
                "ami_master_updated_at": {"$exists": True}
            },
            {
                "_id": 0,
                "meter_serial": 1,
                "name": 1,
                "october_last_value": 1,
                "period_first_value": 1,
                "last_reading": 1,
                "sort_order": 1
            }
        )

        if not doc:
            not_found.append(meter_serial)
            continue

        old_value = doc.get("october_last_value")
        last_value = (doc.get("last_reading") or {}).get("value")

        rows_report.append({
            "sort_order": doc.get("sort_order"),
            "meter_serial": meter_serial,
            "name": doc.get("name"),
            "old_first_value": old_value,
            "new_first_value": first_value,
            "last_reading_value": last_value,
            "raw_csv_value": raw_value,
        })

        if args.apply:
            result = meters.update_one(
                {
                    "meter_serial": meter_serial,
                    "ami_master_updated_at": {"$exists": True}
                },
                {
                    "$set": {
                        "october_last_value": first_value,
                        "period_first_value": first_value,
                        "period_first_updated_at": datetime.now(timezone.utc),
                        "period_first_source": "manual_invoice",
                    }
                }
            )
            if result.matched_count:
                updated += 1

with open(args.report, "w", newline="", encoding="utf-8-sig") as f:
    fieldnames = [
        "sort_order",
        "meter_serial",
        "name",
        "old_first_value",
        "new_first_value",
        "last_reading_value",
        "raw_csv_value",
    ]
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for r in sorted(rows_report, key=lambda x: (x.get("sort_order") is None, x.get("sort_order") or 999999)):
        writer.writerow(r)

mode = "APPLY" if args.apply else "DRY-RUN"
print(f"Mod: {mode}")
print(f"CSV: {args.csv}")
print(f"Rapor: {args.report}")
print(f"Okunan/geçerli satır: {len(rows_report)}")
print(f"Güncellenen sayaç: {updated}")
print(f"Bulunamayan sayaç: {len(not_found)}")
print(f"Atlanan satır: {len(skipped)}")

print("\nİlk 20 kontrol satırı:")
for r in sorted(rows_report, key=lambda x: (x.get("sort_order") is None, x.get("sort_order") or 999999))[:20]:
    print(
        f"{r.get('sort_order')} | {r['meter_serial']} | {r['name']} | "
        f"eski={r['old_first_value']} -> yeni={r['new_first_value']} | "
        f"son_okuma={r['last_reading_value']} | csv={r['raw_csv_value']}"
    )

if not_found:
    print("\nDB’de aktif AMI master içinde bulunamayan sayaçlar:")
    for s in not_found:
        print("-", s)

if skipped:
    print("\nAtlanan satırlar:")
    for s in skipped:
        print("-", s)
