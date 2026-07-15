import csv
from pymongo import MongoClient

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "amimavialp"
CSV_PATH = "/opt/kolayveri_ami/first_readings.csv"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
meters = db["meters"]

updated = 0
not_found = []

with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        meter_serial = str(row["meter_serial"]).strip()
        first_value = float(row["first_value"])

        result = meters.update_one(
            {"meter_serial": meter_serial},
            {
                "$set": {
                    "october_last_value": first_value
                }
            }
        )

        if result.matched_count:
            updated += 1
        else:
            not_found.append(meter_serial)

print(f"✅ Güncellenen sayaç: {updated}")

if not_found:
    print("\n❌ DB’de bulunamayan sayaçlar:")
    for s in not_found:
        print("-", s)
