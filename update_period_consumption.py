from datetime import datetime
from pymongo import MongoClient

MONGO_URI = "mongodb+srv://onderoksuztepe_db:OnderKolayveri2025@kolayveri.t0lyzeu.mongodb.net/"
DB_NAME = "amimavialp"
METERS_COLL = "meters"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
meters_col = db[METERS_COLL]


def main():
    meters = list(meters_col.find({}))
    print(f"{len(meters)} sayaç üzerinde dönem tüketimi hesaplanacak.")

    for m in meters:
        serial = m.get("meter_serial")
        multiplier = m.get("multiplier", 1)
        october_last = m.get("october_last_value")
        last_reading = m.get("last_reading")

        if october_last is None or last_reading is None:
            print(f"{serial}: october_last_value veya last_reading yok, atlandı.")
            continue

        last_value = last_reading.get("value")
        if last_value is None:
            print(f"{serial}: last_reading.value yok, atlandı.")
            continue

        try:
            october_last_f = float(october_last)
            last_value_f = float(last_value)
            multiplier_f = float(multiplier)
        except Exception as e:
            print(f"{serial}: float çevrim hatası: {e}")
            continue

        raw_delta = last_value_f - october_last_f
        period_kwh = raw_delta * multiplier_f

        result = meters_col.update_one(
            {"_id": m["_id"]},
            {
                "$set": {
                    "period_previous_reading_value": october_last_f,
                    "period_last_reading_value": last_value_f,
                    "period_raw_delta": raw_delta,
                    "period_consumption_kwh": period_kwh,
                    "period_updated_at": datetime.utcnow(),
                }
            },
        )

        print(
            f"{serial}: raw_delta={raw_delta}, multiplier={multiplier_f}, "
            f"period_consumption_kwh={period_kwh} (modified={result.modified_count})"
        )


if __name__ == "__main__":
    main()

