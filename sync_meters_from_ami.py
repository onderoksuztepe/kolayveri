from datetime import datetime, timezone
import requests
from pymongo import MongoClient
from requests.exceptions import RequestException, HTTPError

# ============================
# KONFİG
# ============================

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "amimavialp"
METERS_COLL = "meters"

AMI_API_BASE = "https://ami.mavialp.com/api/v1/clients/company"
AMI_TOKEN = "545b04361cd3fae5a416307251fdf3a03ec8f3d2e67f7d715a"
AMI_COMPANY_CODE = "C2WPLO5MR20JXY5LL"

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
meters_col = db[METERS_COLL]


def build_headers():
    return {
        "Authorization": f"Bearer {AMI_TOKEN}",
        "Ami-Company-Code": AMI_COMPANY_CODE,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def fetch_all_meters():
    """
    AMI sayaç listesini sayfalı şekilde çekmeye çalışır.
    Varsayım: liste endpoint'i /meters/
    """
    url = f"{AMI_API_BASE}/meters/"
    headers = build_headers()

    all_items = []
    next_url = url
    page = 1

    while next_url:
        print(f"Sayfa çekiliyor: {page} -> {next_url}")
        resp = requests.get(next_url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict):
            results = data.get("results", [])
            next_url = data.get("next")
        elif isinstance(data, list):
            results = data
            next_url = None
        else:
            raise ValueError(f"Beklenmeyen response tipi: {type(data)}")

        all_items.extend(results)
        page += 1

    return all_items


def normalize_meter(item: dict):
    """
    Farklı field isimlerine toleranslı normalize.
    """
    meter_serial = (
        item.get("meter_serial")
        or item.get("serial")
        or item.get("meterNo")
        or item.get("meter_number")
        or item.get("code")
    )

    if not meter_serial:
        return None

    name = (
        item.get("name")
        or item.get("title")
        or item.get("meter_name")
        or item.get("subscriber_name")
        or ""
    )

    multiplier = (
        item.get("multiplier")
        or item.get("coefficient")
        or item.get("ratio")
        or 1
    )

    group_id = (
        item.get("group_id")
        or item.get("group")
        or item.get("project_id")
        or ""
    )

    status = (
        item.get("status")
        or "ok"
    )

    try:
        multiplier = float(multiplier)
    except Exception:
        multiplier = 1.0

    return {
        "meter_serial": str(meter_serial).strip(),
        "name": str(name).strip(),
        "multiplier": multiplier,
        "group_id": str(group_id).strip(),
        "status": str(status).strip(),
        "raw_master": item,
    }


def upsert_meter(meter: dict):
    now = datetime.now(timezone.utc)

    result = meters_col.update_one(
        {"meter_serial": meter["meter_serial"]},
        {
            "$set": {
                "name": meter["name"],
                "multiplier": meter["multiplier"],
                "group_id": meter["group_id"],
                "status": meter["status"],
                "raw_master": meter["raw_master"],
                "updated_at": now,
            },
            "$setOnInsert": {
                "meter_serial": meter["meter_serial"],
                "created_at": now,
            },
        },
        upsert=True,
    )
    return result


def main():
    inserted = 0
    updated = 0
    skipped = 0

    try:
        items = fetch_all_meters()
        print(f"AMI'den gelen toplam sayaç kaydı: {len(items)}")
    except HTTPError as e:
        print(f"HTTP hata: {e}")
        raise
    except RequestException as e:
        print(f"Request hata: {e}")
        raise

    for item in items:
        meter = normalize_meter(item)
        if not meter:
            skipped += 1
            continue

        existing = meters_col.find_one({"meter_serial": meter["meter_serial"]}, {"_id": 1})
        upsert_meter(meter)

        if existing:
            updated += 1
        else:
            inserted += 1

    print(f"Yeni eklenen: {inserted}")
    print(f"Güncellenen: {updated}")
    print(f"Atlanan: {skipped}")


if __name__ == "__main__":
    main()
