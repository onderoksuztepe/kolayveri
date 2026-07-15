import csv
import re
from pymongo import MongoClient

DB_NAME = "amimavialp"
REPORT = "dryrun_ami_update_report.csv"

client = MongoClient("mongodb://127.0.0.1:27017")
db = client[DB_NAME]

def only_digits(x):
    return re.sub(r"\D", "", str(x or ""))

def doc_text(doc):
    return " ".join(str(v) for v in doc.values())

meters = list(db.meters.find({}))
readings = list(db.readings.find({}))

not_found = []
with open(REPORT, "r", encoding="utf-8-sig", newline="") as f:
    reader = csv.reader(f)
    for row in reader:
        if not row:
            continue
        if row[0] == "NOT_FOUND":
            raw = row[1]
            norm = row[2]
            name = row[7] if len(row) > 7 else ""
            not_found.append((raw, norm, name))

print(f"NOT_FOUND sayısı: {len(not_found)}")
print("-" * 100)

for raw, norm, name in not_found:
    raw_d = only_digits(raw)
    norm_d = only_digits(norm)

    meter_hits = []
    for m in meters:
        txt = doc_text(m)
        txt_d = only_digits(txt)
        if raw in txt or norm in txt or (raw_d and raw_d in txt_d) or (norm_d and norm_d in txt_d):
            meter_hits.append(m)

    reading_hits = []
    for r in readings:
        txt = doc_text(r)
        txt_d = only_digits(txt)
        if raw in txt or norm in txt or (raw_d and raw_d in txt_d) or (norm_d and norm_d in txt_d):
            reading_hits.append(r)
            if len(reading_hits) >= 3:
                break

    print(f"AMI: {raw} | KEY: {norm} | AD: {name}")
    print(f"  meters içinde olası eşleşme: {len(meter_hits)}")
    for h in meter_hits[:3]:
        print("   ", {k: h.get(k) for k in h.keys() if k in ['meter_serial','serial','meter_no','meter_number','name','device_no','group_id','multiplier','last_reading']})
        print("    full:", h)
    print(f"  readings içinde olası eşleşme ilk kontrol: {len(reading_hits)}")
    for h in reading_hits[:1]:
        print("    reading sample:", h)
    print("-" * 100)
