import csv
from datetime import datetime, timezone
from pymongo import MongoClient

DRY_RUN = False

client = MongoClient("mongodb://localhost:27017")
db = client["amimavialp"]
meters = db["meters"]

def parse_number(v):
    if v is None or str(v).strip() == "":
        return None
    return float(str(v).replace(",", "").strip())

def parse_date(v):
    return str(v).strip()

updated = []
not_found = []

with open("meter_update.csv", "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        serial = str(row["meter_serial"]).strip()
        doc = meters.find_one({"meter_serial": serial})

        if not doc:
            not_found.append(serial)
            continue

        update_data = {
            "name": str(row["name"]).strip(),
            "base_index_value": parse_number(row["base_index_value"]),
            "base_index_date": parse_date(row["base_index_date"]),
            "multiplier": parse_number(row["multiplier"]),
            "updated_at": datetime.now(timezone.utc),
        }

        updated.append({
            "meter_serial": serial,
            "old_name": doc.get("name", ""),
            "new_name": update_data["name"],
            "base_index_value": update_data["base_index_value"],
            "base_index_date": update_data["base_index_date"],
            "multiplier": update_data["multiplier"],
        })

        if not DRY_RUN:
            meters.update_one(
                {"meter_serial": serial},
                {"$set": update_data}
            )

print("DRY_RUN:", DRY_RUN)
print("Güncellenecek:", len(updated))
print("Bulunamayan:", len(not_found))

print("\nİlk 10 değişiklik:")
for r in updated[:10]:
    print(r)
