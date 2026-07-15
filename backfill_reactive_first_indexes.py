import argparse
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import psycopg2
import requests

from ami_to_mongo import AMI_API_BASE, AMI_TOKEN, AMI_COMPANY_CODE, meters_col


DB_CONFIG = {
    "host": "127.0.0.1",
    "dbname": "kolayveri_db",
    "user": "kolayveri_user",
    "password": "Kv2026ChangeMe123",
    "port": 5432,
}


def normalize_serial(serial):
    if serial is None:
        return None
    s = str(serial).strip()
    upper = s.upper()

    for prefix in ("MSY", "AEL", "VIK"):
        if upper.startswith(prefix):
            s = s[len(prefix):]
            upper = s.upper()
            break

    if s.isdigit():
        s = s.lstrip("0") or "0"

    return s


def parse_obis(read_data, code):
    if not read_data:
        return None
    m = re.search(rf"{re.escape(code)}\(([\d\.]+)\*", read_data)
    if not m:
        return None
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return None


def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def fetch_first_reactive(api_serial, start_dt, end_dt):
    url = f"{AMI_API_BASE}/meters/{api_serial}/read/data/"
    headers = {
        "Authorization": f"Bearer {AMI_TOKEN}",
        "Ami-Company-Code": AMI_COMPANY_CODE,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    params = {
        "readed_at__gte": start_dt.isoformat().replace("+00:00", "Z"),
        "readed_at__lte": end_dt.isoformat().replace("+00:00", "Z"),
        "page_size": 1000,
    }

    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    results = data.get("results", [])

    candidates = []
    for row in results:
        raw = row.get("raw_data") or {}
        ts = parse_ts(raw.get("readTimestamp") or row.get("readed_at"))
        read_data = raw.get("readData") or ""

        ind = parse_obis(read_data, "5.8.0")
        cap = parse_obis(read_data, "8.8.0")

        if ts and (ind is not None or cap is not None):
            candidates.append((ts, ind, cap))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="2026-06")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    found = 0
    updated = 0
    missing_pg = 0
    no_reading = 0
    errors = 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, start_date FROM periods WHERE period_code = %s",
                (args.period,),
            )
            period = cur.fetchone()
            if not period:
                raise RuntimeError(f"Dönem bulunamadı: {args.period}")

            period_id, start_date = period
            start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
            end_dt = start_dt + timedelta(days=args.days)

            print("Dönem:", args.period)
            print("Aralık:", start_dt.isoformat(), "→", end_dt.isoformat())
            print("Mod:", "APPLY - DB'YE YAZAR" if args.apply else "DRY-RUN - DB'YE YAZMAZ")
            print()

            mongo_meters = list(
                meters_col.find(
                    {"ami_master_updated_at": {"$exists": True}},
                    {"meter_serial": 1, "name": 1, "status": 1, "_id": 0},
                ).sort([("sort_order", 1), ("meter_serial", 1)])
            )

            for m in mongo_meters:
                api_serial = m.get("meter_serial")
                norm = normalize_serial(api_serial)

                cur.execute(
                    """
                    SELECT id, meter_serial, meter_name
                    FROM meters
                    WHERE (
                        CASE
                            WHEN meter_serial ~ '^[0-9]+$'
                            THEN COALESCE(NULLIF(ltrim(meter_serial, '0'), ''), '0')
                            ELSE meter_serial
                        END = %s
                    )
                    LIMIT 1
                    """,
                    (norm,),
                )
                pg_meter = cur.fetchone()
                if not pg_meter:
                    missing_pg += 1
                    continue

                meter_id, pg_serial, meter_name = pg_meter

                try:
                    first = fetch_first_reactive(api_serial, start_dt, end_dt)
                except Exception as e:
                    errors += 1
                    print("ERR", api_serial, str(e)[:160])
                    continue

                if not first:
                    no_reading += 1
                    continue

                ts, ind, cap = first
                found += 1

                print(
                    "FOUND",
                    api_serial,
                    ts.isoformat(),
                    "ind:", ind,
                    "cap:", cap,
                    "-",
                    meter_name,
                )

                if args.apply:
                    cur.execute(
                        """
                        UPDATE meter_period_indexes
                        SET
                            first_inductive_index = COALESCE(first_inductive_index, %s),
                            first_capacitive_index = COALESCE(first_capacitive_index, %s),
                            updated_at = now()
                        WHERE meter_id = %s
                          AND period_id = %s
                        """,
                        (ind, cap, meter_id, period_id),
                    )
                    updated += cur.rowcount

            if args.apply:
                conn.commit()
            else:
                conn.rollback()

            print()
            print("ÖZET")
            print("Mongo sayaç:", len(mongo_meters))
            print("Bulunan ilk reaktif:", found)
            print("Güncellenen:", updated)
            print("PG eşleşmeyen:", missing_pg)
            print("Okuma bulunamayan:", no_reading)
            print("Hata:", errors)

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
