import json
import os
from decimal import Decimal, InvalidOperation

import psycopg2
import requests


DB_CONFIG = {
    "host": "127.0.0.1",
    "dbname": "kolayveri_db",
    "user": "kolayveri_user",
    "password": "Kv2026ChangeMe123",
    "port": 5432,
}

API_URL = "http://127.0.0.1:8000/api/meters/last-vs-previous"
PERIOD_CODE = os.getenv("KOLAYVERI_PERIOD_CODE")


def parse_decimal(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    text = text.replace(".", "").replace(",", ".") if "," in text else text
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def normalize_serial(serial):
    if serial is None:
        return None
    s = str(serial).strip()
    upper = s.upper()

    for prefix in ("MSY", "AEL"):
        if upper.startswith(prefix):
            s = s[len(prefix):]
            upper = s.upper()
            break

    if s.isdigit():
        s = s.lstrip("0") or "0"

    return s


def main():
    print("API verisi çekiliyor...")
    response = requests.get(API_URL, timeout=120)
    response.raise_for_status()
    rows = response.json()

    print(f"API kayıt sayısı: {len(rows)}")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    matched = 0
    unmatched = 0
    calculated = 0
    errors = 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO automation_runs (job_name, status, message)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                ("sync_last_readings_to_pg", "running", "AMI son okuma senkronizasyonu başladı"),
            )
            run_id = cur.fetchone()[0]

            if PERIOD_CODE:
                cur.execute(
                    "SELECT id, period_code FROM periods WHERE period_code = %s",
                    (PERIOD_CODE,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, period_code
                    FROM periods
                    WHERE status = 'open'
                    ORDER BY start_date DESC
                    LIMIT 1
                    """
                )

            period = cur.fetchone()
            if not period:
                raise RuntimeError("Açık dönem bulunamadı. periods tablosunda status='open' dönem olmalı.")

            period_id = period[0]
            active_period_code = period[1]
            print(f"Aktif dönem: {active_period_code}")

            for item in rows:
                api_serial = item.get("meter_serial")
                meter_serial = normalize_serial(api_serial)

                last_read_time = item.get("last_read_time")
                last_value = parse_decimal(item.get("last_read_value"))
                last_inductive_value = parse_decimal(item.get("last_inductive_value"))
                last_capacitive_value = parse_decimal(item.get("last_capacitive_value"))
                api_previous_value = parse_decimal(item.get("previous_end_value"))

                cur.execute(
                    """
                    SELECT m.id, m.multiplier
                    FROM meters m
                    LEFT JOIN meter_aliases ma ON ma.meter_id = m.id
                    WHERE (
                        CASE
                            WHEN m.meter_serial ~ '^[0-9]+$'
                            THEN COALESCE(NULLIF(ltrim(m.meter_serial, '0'), ''), '0')
                            ELSE m.meter_serial
                        END = %s
                        OR ma.alias_serial = %s
                    )
                    LIMIT 1
                    """,
                    (meter_serial, meter_serial),
                )
                meter = cur.fetchone()

                if not meter:
                    unmatched += 1
                    continue

                meter_id, multiplier = meter
                multiplier = Decimal(str(multiplier or 1))
                matched += 1

                cur.execute(
                    """
                    INSERT INTO ami_last_readings (
                        meter_id,
                        reading_time,
                        active_index,
                        inductive_index,
                        capacitive_index,
                        raw_json,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (meter_id) DO UPDATE SET
                        reading_time = EXCLUDED.reading_time,
                        active_index = EXCLUDED.active_index,
                        inductive_index = COALESCE(EXCLUDED.inductive_index, ami_last_readings.inductive_index),
                        capacitive_index = COALESCE(EXCLUDED.capacitive_index, ami_last_readings.capacitive_index),
                        raw_json = EXCLUDED.raw_json,
                        updated_at = now()
                    """,
                    (
                        meter_id,
                        last_read_time,
                        last_value,
                        last_inductive_value,
                        last_capacitive_value,
                        json.dumps(item, ensure_ascii=False),
                    ),
                )

                # ÖNEMLİ:
                # İlk endeks varsa ASLA API previous_end_value ile ezmiyoruz.
                # Hesaplamada da API previous_end_value değil, DB'deki dönem ilk endeksi kullanılacak.
                cur.execute(
                    """
                    UPDATE meter_period_indexes
                    SET
                        first_active_index = COALESCE(first_active_index, %s),
                        last_active_index = %s,
                        last_inductive_index = COALESCE(%s, last_inductive_index),
                        last_capacitive_index = COALESCE(%s, last_capacitive_index),
                        first_source = CASE
                            WHEN first_active_index IS NULL THEN 'api_previous_end'
                            ELSE first_source
                        END,
                        last_source = 'ami_api',
                        updated_at = now()
                    WHERE meter_id = %s
                      AND period_id = %s
                    RETURNING
                        first_active_index,
                        last_active_index,
                        first_inductive_index,
                        last_inductive_index,
                        first_capacitive_index,
                        last_capacitive_index
                    """,
                    (api_previous_value, last_value, last_inductive_value, last_capacitive_value, meter_id, period_id),
                )

                idx = cur.fetchone()

                if not idx:
                    cur.execute(
                        """
                        INSERT INTO meter_period_indexes (
                            meter_id,
                            period_id,
                            first_active_index,
                            last_active_index,
                            last_inductive_index,
                            last_capacitive_index,
                            first_source,
                            last_source,
                            updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, 'api_previous_end', 'ami_api', now())
                        RETURNING
                            first_active_index,
                            last_active_index,
                            first_inductive_index,
                            last_inductive_index,
                            first_capacitive_index,
                            last_capacitive_index
                        """,
                        (meter_id, period_id, api_previous_value, last_value, last_inductive_value, last_capacitive_value),
                    )
                    idx = cur.fetchone()

                (
                    period_first_value,
                    period_last_value,
                    period_first_inductive_value,
                    period_last_inductive_value,
                    period_first_capacitive_value,
                    period_last_capacitive_value,
                ) = idx

                status = "calculated"
                error_message = None
                active_consumption = None
                inductive_consumption = None
                capacitive_consumption = None
                inductive_ratio_pct = None
                capacitive_ratio_pct = None
                reactive_status = "missing_reactive_index"

                if period_first_value is None:
                    status = "missing_first_index"
                    error_message = "Dönem ilk endeksi yok"
                    errors += 1
                elif period_last_value is None:
                    status = "missing_last_index"
                    error_message = "Dönem son endeksi yok"
                    errors += 1
                elif period_last_value < period_first_value:
                    status = "negative_consumption"
                    error_message = f"Son endeks ilk endeksten küçük: {period_last_value} < {period_first_value}"
                    errors += 1
                else:
                    active_consumption = (period_last_value - period_first_value) * multiplier
                    calculated += 1

                if (
                    period_first_inductive_value is not None
                    and period_last_inductive_value is not None
                ):
                    if period_last_inductive_value >= period_first_inductive_value:
                        inductive_consumption = (period_last_inductive_value - period_first_inductive_value) * multiplier
                    else:
                        reactive_status = "negative_reactive_index"

                if (
                    period_first_capacitive_value is not None
                    and period_last_capacitive_value is not None
                ):
                    if period_last_capacitive_value >= period_first_capacitive_value:
                        capacitive_consumption = (period_last_capacitive_value - period_first_capacitive_value) * multiplier
                    else:
                        reactive_status = "negative_reactive_index"

                if active_consumption and active_consumption != 0:
                    if inductive_consumption is not None:
                        inductive_ratio_pct = (inductive_consumption / active_consumption) * Decimal("100")
                    if capacitive_consumption is not None:
                        capacitive_ratio_pct = (capacitive_consumption / active_consumption) * Decimal("100")

                if reactive_status != "negative_reactive_index":
                    if inductive_consumption is not None or capacitive_consumption is not None:
                        reactive_status = "calculated"

                cur.execute(
                    """
                    INSERT INTO meter_period_calculations (
                        meter_id,
                        period_id,
                        active_consumption,
                        inductive_consumption,
                        capacitive_consumption,
                        inductive_ratio_pct,
                        capacitive_ratio_pct,
                        reactive_status,
                        calculation_status,
                        error_message,
                        calculated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (meter_id, period_id) DO UPDATE SET
                        active_consumption = EXCLUDED.active_consumption,
                        inductive_consumption = EXCLUDED.inductive_consumption,
                        capacitive_consumption = EXCLUDED.capacitive_consumption,
                        inductive_ratio_pct = EXCLUDED.inductive_ratio_pct,
                        capacitive_ratio_pct = EXCLUDED.capacitive_ratio_pct,
                        reactive_status = EXCLUDED.reactive_status,
                        calculation_status = EXCLUDED.calculation_status,
                        error_message = EXCLUDED.error_message,
                        calculated_at = now()
                    """,
                    (
                        meter_id,
                        period_id,
                        active_consumption,
                        inductive_consumption,
                        capacitive_consumption,
                        inductive_ratio_pct,
                        capacitive_ratio_pct,
                        reactive_status,
                        status,
                        error_message,
                    ),
                )

                if status == "calculated":
                    cur.execute(
                        """
                        UPDATE alerts
                        SET status = 'resolved',
                            updated_at = now()
                        WHERE period_id = %s
                          AND meter_id = %s
                          AND status = 'open'
                          AND alert_type IN (
                              'negative_consumption',
                              'missing_first_index',
                              'missing_last_index'
                          )
                        """,
                        (period_id, meter_id),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO alerts (
                            period_id,
                            meter_id,
                            alert_type,
                            severity,
                            message,
                            status,
                            created_at,
                            updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, 'open', now(), now())
                        ON CONFLICT (period_id, meter_id, alert_type)
                        WHERE status = 'open'
                        DO UPDATE SET
                            severity = EXCLUDED.severity,
                            message = EXCLUDED.message,
                            updated_at = now()
                        """,
                        (
                            period_id,
                            meter_id,
                            status,
                            "high" if status == "negative_consumption" else "medium",
                            error_message,
                        ),
                    )

            cur.execute(
                """
                UPDATE automation_runs
                SET status = %s,
                    finished_at = now(),
                    message = %s
                WHERE id = %s
                """,
                (
                    "success",
                    f"period={active_period_code}, matched={matched}, unmatched={unmatched}, calculated={calculated}, errors={errors}",
                    run_id,
                ),
            )

        conn.commit()

    except Exception as exc:
        conn.rollback()
        raise exc

    finally:
        conn.close()

    print("Bitti.")
    print(f"Eşleşen sayaç: {matched}")
    print(f"Eşleşmeyen sayaç: {unmatched}")
    print(f"Hesaplanan: {calculated}")
    print(f"Hatalı/bekleyen: {errors}")


if __name__ == "__main__":
    main()
