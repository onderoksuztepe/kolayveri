from pymongo import MongoClient
from datetime import datetime

client = MongoClient("mongodb://127.0.0.1:27017")
db = client["amimavialp"]

mappings = [
    ("VIK2778117", "02778117"),
    ("MSY810044631", "AEL810044631"),
    ("MSY810026991", "AEL810026991"),
    ("MSY810026769", "AEL810026769"),
    ("MSY810028051", "AEL810028051"),
    ("MSY810043851", "AEL810043851"),
    ("MSY810044603", "AEL810044603"),
    ("MSY810027046", "AEL810027046"),
]

print("Çakışma kontrolü...")
for old, new in mappings:
    old_count = db.meters.count_documents({"meter_serial": old})
    new_count = db.meters.count_documents({"meter_serial": new})
    print(f"{old} -> {new} | old_count={old_count} | new_count={new_count}")
    if new_count > 0:
        raise SystemExit(f"DUR: {new} zaten meters içinde var. Çakışma riski.")

print("Güncelleme başlıyor...")
for old, new in mappings:
    result = db.meters.update_one(
        {"meter_serial": old},
        {
            "$set": {
                "meter_serial": new,
                "meter_serial_raw": new,
                "meter_key_normalized": ''.join(ch for ch in new if ch.isdigit()) if new[0].isdigit() else new,
                "status": "pending_sync",
                "status_message": f"meter_serial corrected from {old} to {new}",
                "updated_at": datetime.utcnow(),
            }
        }
    )
    print(f"{old} -> {new} | matched={result.matched_count} | modified={result.modified_count}")

print("Bitti.")
