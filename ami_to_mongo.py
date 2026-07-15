import re
import time
from datetime import datetime, timezone

import requests
from pymongo import MongoClient
from requests.exceptions import HTTPError, ReadTimeout, RequestException

from datetime import datetime, timezone, timedelta

def _build_date_filters():
    """
    AMI için tarih filtresi üretir.
    Son 2 gün (UTC) aralığını kullanıyoruz.
    """
    now_utc = datetime.now(timezone.utc)
    start_utc = (now_utc - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "readed_at__gte": start_utc.strftime(fmt),
        "readed_at__lte": now_utc.strftime(fmt)
    }

# ============================
#  KONFİGÜRASYON
# ============================

# MongoDB
MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "amimavialp"
METERS_COLL = "meters"
READINGS_COLL = "readings"

# AMI Mavialp
AMI_API_BASE = "https://ami.mavialp.com/api/v1/clients/company"
AMI_TOKEN = "545b04361cd3fae5a416307251fdf3a03ec8f3d2e67f7d715a"          # Örn: 545b0...
AMI_COMPANY_CODE = "C2WPLO5MR20JXY5LL"    # Örn: C2WPLO5MR20JXY5LL

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[DB_NAME]
meters_col = db[METERS_COLL]
readings_col = db[READINGS_COLL]


# ============================
#  YARDIMCI FONKSİYONLAR
# ============================

def get_all_meters():
    """Sadece AMI master listesinde aktif olan sayaçları döner."""
    return list(
        meters_col.find(
            {"ami_master_updated_at": {"$exists": True}},
            {"meter_serial": 1, "_id": 0}
        )
    )


def fetch_latest_reading(meter_serial: str):
    """
    AMI'den ilgili sayaç için okuma verisini çeker.
    İlk sayfadaki ilk kaydı 'en güncel okuma' olarak kabul eder.
    raw_data.readTimestamp ve raw_data.readData içinden 1.8.0 değerini alır.
    """
    url = f"{AMI_API_BASE}/meters/{meter_serial}/read/data/"

    headers = {
        "Authorization": f"Bearer {AMI_TOKEN}",
        "Ami-Company-Code": AMI_COMPANY_CODE,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    params = _build_date_filters()
    resp = requests.get(url, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", [])
    if not results:
        # results boş ise None döndürüyoruz; status'i main() içinde yazacağız
        return None

    # Varsayım: results[0] = en güncel okuma
    latest = results[0]
    raw = latest.get("raw_data", {})

    # Zaman damgası
    ts_str = raw.get("readTimestamp")
    if not ts_str:
        raise ValueError(f"{meter_serial}: raw_data.readTimestamp yok.")

    # Örn: "2025-11-25T19:00:52+00:00"
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

    # readData içinden OBIS endekslerini çek
    read_data_str = raw.get("readData", "")

    def obis_value(code: str):
        match = re.search(rf"{re.escape(code)}\(([\d\.]+)\*", read_data_str)
        if not match:
            return None
        return float(match.group(1))

    value = obis_value("1.8.0")
    inductive_value = obis_value("5.8.0")
    capacitive_value = obis_value("8.8.0")

    if value is None:
        raise ValueError(f"{meter_serial}: 1.8.0 değeri readData içinde bulunamadı.")

    return {
        "time": ts,
        "value": value,
        "inductive_value": inductive_value,
        "capacitive_value": capacitive_value,
        "raw": latest,
    }


def save_reading(meter_serial: str, reading: dict):
    """
    readings koleksiyonuna log kaydı ekler,
    meters.last_reading alanını günceller.
    """
    # readings: aynı sayaç + aynı zaman varsa tekrar insert etmesin
    readings_col.update_one(
        {
            "meter_serial": meter_serial,
            "time": reading["time"],
        },
        {
            "$setOnInsert": {
                "meter_serial": meter_serial,
                "time": reading["time"],
                "value": reading["value"],
                "inductive_value": reading.get("inductive_value"),
                "capacitive_value": reading.get("capacitive_value"),
                "raw": reading["raw"],
                "created_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )

    # meters.last_reading güncelle
    meters_col.update_one(
        {"meter_serial": meter_serial},
        {
            "$set": {
                "last_reading": {
                    "time": reading["time"],
                    "value": reading["value"],
                    "inductive_value": reading.get("inductive_value"),
                    "capacitive_value": reading.get("capacitive_value"),
                },
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


def update_meter_status(meter_serial: str, status: str, message: str = None):
    """
    Sayaç durumunu meters koleksiyonunda günceller.
    status: ok, no_readings, not_found, timeout, http_error, error ...
    """
    meters_col.update_one(
        {"meter_serial": meter_serial},
        {
            "$set": {
                "status": status,
                "status_message": message,
                "status_updated_at": datetime.now(timezone.utc),
            }
        },
    )


# ============================
#  ANA ÇALIŞMA
# ============================

def main():
    meters = get_all_meters()
    print(f"{len(meters)} sayaç bulundu.")

    for m in meters:
        serial = m["meter_serial"]
        try:
            reading = fetch_latest_reading(serial)

            if reading is None:
                print(f"{serial}: results boş, okuma yok.")
                update_meter_status(serial, "no_readings", "AMI results boş, okuma yok.")
                continue

            # Başarılı okuma
            save_reading(serial, reading)
            print(f"{serial}: {reading['time']} -> {reading['value']} kWh")
            update_meter_status(
                serial,
                "ok",
                f"Son okuma {reading['time']} / {reading['value']} kWh",
            )

        except HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            msg = str(e)

            if code == 404:
                status = "not_found"
            elif code == 401:
                status = "unauthorized"
            else:
                status = "http_error"

            print(f"{serial} HATA (HTTP {code}): {msg}")
            update_meter_status(serial, status, msg)

        except ReadTimeout as e:
            msg = f"Read timeout: {e}"
            print(f"{serial} HATA (timeout): {msg}")
            update_meter_status(serial, "timeout", msg)

        except RequestException as e:
            msg = f"RequestException: {e}"
            print(f"{serial} HATA (request): {msg}")
            update_meter_status(serial, "error", msg)

        except Exception as e:
            msg = f"Genel hata: {e}"
            print(f"{serial} HATA: {msg}")
            update_meter_status(serial, "error", msg)

        # API'yı boğmamak için küçük gecikme
        time.sleep(0.2)


if __name__ == "__main__":
    main()

