import csv
import re
from datetime import datetime, timezone
from pymongo import MongoClient

# =========================
# AYARLAR
# =========================
MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "amimavialp"
METERS_COLL = "meters"

AMI_FILE = "ami_meters.csv"
AMI_SERIAL_COLUMN = "ami_meters_serial"

# numeric-only AMI kayıtlarını otomatik ekleme
AUTO_ADD_NUMERIC_ONLY = False

# gerçekten DB güncellemek için False yap
DRY_RUN = False


# =========================
# YARDIMCI FONKSİYONLAR
# =========================
def now_dt():
    return datetime.now(timezone.utc)

def now_iso():
    return now_dt().isoformat()

def normalize(v):
    if v is None:
        return ""
    v = str(v).strip().upper()
    v = re.sub(r"[\s\-/]+", "", v)
    v = re.sub(r"^[A-Z]+", "", v)   # MSY300... -> 300...
    return v

def looks_numeric_only(v):
    v = str(v).strip()
    return v.isdigit()

def looks_prefixed(v):
    v = str(v).strip().upper()
    return bool(re.match(r"^[A-Z]+[0-9]+$", v))

def write_csv(filename, rows):
    if not rows:
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            f.write("")
        return

    # tüm satırlardaki tüm kolonları topla
    all_keys = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                all_keys.append(k)

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# =========================
# AMI CSV OKU
# =========================
ami_rows = []
with open(AMI_FILE, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        raw_serial = (row.get(AMI_SERIAL_COLUMN) or "").strip()
        if not raw_serial:
            continue

        ami_rows.append({
            "ami_meter_serial": raw_serial,
            "normalized_serial": normalize(raw_serial),
            "is_numeric_only": looks_numeric_only(raw_serial),
            "is_prefixed": looks_prefixed(raw_serial),
        })

ami_map = {
    r["normalized_serial"]: r
    for r in ami_rows
    if r["normalized_serial"]
}

# =========================
# MONGO BAĞLANTI
# =========================
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
meters = db[METERS_COLL]

mongo_docs = list(meters.find({}))
mongo_map = {}

for doc in mongo_docs:
    meter_serial = str(doc.get("meter_serial", "")).strip()
    norm = normalize(meter_serial)
    if norm:
        mongo_map[norm] = doc

# =========================
# KARŞILAŞTIRMA
# =========================
matched = []
to_add = []
to_inactivate = []
manual_review = []

# 1) AMI'de olup Mongo'da olmayanlar
for norm, ami in ami_map.items():
    if norm in mongo_map:
        matched.append({
            "source": "matched",
            "normalized_serial": norm,
            "mongo_meter_serial": mongo_map[norm].get("meter_serial", ""),
            "mongo_name": mongo_map[norm].get("name", ""),
            "mongo_status": mongo_map[norm].get("status", ""),
            "ami_meter_serial": ami["ami_meter_serial"],
        })
    else:
        if ami["is_numeric_only"] and not AUTO_ADD_NUMERIC_ONLY:
            manual_review.append({
                "source": "ami",
                "reason": "AMI numeric-only record; auto-add disabled",
                "ami_meter_serial": ami["ami_meter_serial"],
                "normalized_serial": ami["normalized_serial"],
                "action": "review_before_add"
            })
        else:
            to_add.append({
                "meter_serial": ami["ami_meter_serial"],
                "name": "",
                "multiplier": 1,
                "status": "new_from_ami",
                "status_message": f"AMI sync add candidate {now_iso()}",
                "created_at": now_dt(),
                "updated_at": now_dt(),
                "group_id": "PORTFOY_1",
            })

# 2) Mongo'da olup AMI'de olmayanlar
for norm, doc in mongo_map.items():
    if norm not in ami_map:
        meter_serial = str(doc.get("meter_serial", "")).strip()
        current_status = str(doc.get("status", "")).strip()

        if looks_numeric_only(meter_serial):
            manual_review.append({
                "source": "mongo",
                "reason": "Mongo numeric-only record missing in AMI; possible typo/mapping issue",
                "mongo_meter_serial": meter_serial,
                "mongo_name": doc.get("name", ""),
                "mongo_status": current_status,
                "normalized_serial": norm,
                "action": "review_before_inactivate"
            })
        else:
            to_inactivate.append({
                "_id": str(doc.get("_id")),
                "meter_serial": meter_serial,
                "name": doc.get("name", ""),
                "old_status": current_status,
                "new_status": "inactive_candidate",
                "reason": "Not found in AMI sync list",
                "updated_at": now_iso(),
            })

# =========================
# VERITABANI GÜNCELLE
# =========================
applied_add = 0
applied_inactivate = 0

if not DRY_RUN:
    for row in to_add:
        existing = meters.find_one({"meter_serial": row["meter_serial"]})
        if not existing:
            meters.insert_one(row)
            applied_add += 1

    for row in to_inactivate:
        meters.update_one(
            {"meter_serial": row["meter_serial"]},
            {
                "$set": {
                    "status": "inactive_candidate",
                    "status_message": row["reason"],
                    "updated_at": now_dt(),
                }
            }
        )
        applied_inactivate += 1

# =========================
# RAPORLAR
# =========================
write_csv("sync_matched.csv", matched)
write_csv("sync_to_add.csv", to_add)
write_csv("sync_to_inactivate.csv", to_inactivate)
write_csv("sync_manual_review.csv", manual_review)

print("=== ÖZET ===")
print("AMI toplam:", len(ami_rows))
print("Mongo toplam:", len(mongo_docs))
print("Eşleşen:", len(matched))
print("Eklenecek:", len(to_add))
print("Pasifleştirilecek:", len(to_inactivate))
print("Manual review:", len(manual_review))
print("DRY_RUN:", DRY_RUN)

if not DRY_RUN:
    print("Uygulanan ekleme:", applied_add)
    print("Uygulanan pasifleştirme:", applied_inactivate)

print("\nÜretilen dosyalar:")
print(" - sync_matched.csv")
print(" - sync_to_add.csv")
print(" - sync_to_inactivate.csv")
print(" - sync_manual_review.csv")