import argparse
import calendar
from datetime import date, timedelta
import psycopg2


DB_CONFIG = {
    "host": "127.0.0.1",
    "dbname": "kolayveri_db",
    "user": "kolayveri_user",
    "password": "Kv2026ChangeMe123",
    "port": 5432,
}


def parse_period_code(period_code: str):
    year_text, month_text = period_code.split("-")
    year = int(year_text)
    month = int(month_text)
    return year, month


def get_current_period_code():
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def get_period_dates(period_code: str):
    year, month = parse_period_code(period_code)
    start_date = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end_date = date(year, month, last_day)
    return start_date, end_date


def get_previous_period_code(period_code: str):
    year, month = parse_period_code(period_code)
    first_day = date(year, month, 1)
    previous_day = first_day - timedelta(days=1)
    return f"{previous_day.year:04d}-{previous_day.month:02d}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", help="Açılacak dönem. Örn: 2026-06")
    args = parser.parse_args()

    target_period_code = args.period or get_current_period_code()
    previous_period_code = get_previous_period_code(target_period_code)
    start_date, end_date = get_period_dates(target_period_code)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO automation_runs (job_name, status, message)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (
                    "open_monthly_period",
                    "running",
                    f"{target_period_code} dönemi açılıyor",
                ),
            )
            run_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO periods (period_code, start_date, end_date, status)
                VALUES (%s, %s, %s, 'open')
                ON CONFLICT (period_code) DO UPDATE SET
                    start_date = EXCLUDED.start_date,
                    end_date = EXCLUDED.end_date,
                    status = 'open'
                RETURNING id
                """,
                (target_period_code, start_date, end_date),
            )
            target_period_id = cur.fetchone()[0]

            cur.execute(
                """
                SELECT id
                FROM periods
                WHERE period_code = %s
                """,
                (previous_period_code,),
            )
            previous_period_row = cur.fetchone()
            previous_period_id = previous_period_row[0] if previous_period_row else None

            # Hedef dönemden eski açık dönemleri kapat.
            cur.execute(
                """
                UPDATE periods
                SET status = 'closed'
                WHERE start_date < %s
                  AND status = 'open'
                """,
                (start_date,),
            )

            # Sayaç-dönem endeks kayıtlarını aç.
            if previous_period_id:
                cur.execute(
                    """
                    INSERT INTO meter_period_indexes (
                        meter_id,
                        period_id,
                        first_active_index,
                        first_inductive_index,
                        first_capacitive_index,
                        first_source,
                        last_source,
                        updated_at
                    )
                    SELECT
                        m.id,
                        %s,
                        prev.last_active_index,
                        prev.last_inductive_index,
                        prev.last_capacitive_index,
                        CASE
                            WHEN prev.last_active_index IS NULL THEN 'manual_required'
                            ELSE 'previous_period_last_index'
                        END,
                        'ami',
                        now()
                    FROM meters m
                    LEFT JOIN meter_period_indexes prev
                        ON prev.meter_id = m.id
                       AND prev.period_id = %s
                    WHERE m.status = 'active'
                    ON CONFLICT (meter_id, period_id) DO NOTHING
                    """,
                    (target_period_id, previous_period_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO meter_period_indexes (
                        meter_id,
                        period_id,
                        first_source,
                        last_source,
                        updated_at
                    )
                    SELECT
                        m.id,
                        %s,
                        'manual_required',
                        'ami',
                        now()
                    FROM meters m
                    WHERE m.status = 'active'
                    ON CONFLICT (meter_id, period_id) DO NOTHING
                    """,
                    (target_period_id,),
                )

            # Hesap kayıtlarını aç.
            cur.execute(
                """
                INSERT INTO meter_period_calculations (
                    meter_id,
                    period_id,
                    calculation_status,
                    calculated_at
                )
                SELECT
                    m.id,
                    %s,
                    'pending',
                    now()
                FROM meters m
                WHERE m.status = 'active'
                ON CONFLICT (meter_id, period_id) DO NOTHING
                """,
                (target_period_id,),
            )

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_meters
                FROM meters
                WHERE status = 'active'
                """
            )
            total_meters = cur.fetchone()[0]

            cur.execute(
                """
                SELECT
                    COUNT(*) AS index_rows,
                    COUNT(*) FILTER (WHERE first_active_index IS NOT NULL) AS copied_first_indexes,
                    COUNT(*) FILTER (WHERE first_active_index IS NULL) AS missing_first_indexes
                FROM meter_period_indexes
                WHERE period_id = %s
                """,
                (target_period_id,),
            )
            index_rows, copied_first_indexes, missing_first_indexes = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*)
                FROM meter_period_calculations
                WHERE period_id = %s
                """,
                (target_period_id,),
            )
            calculation_rows = cur.fetchone()[0]

            message = (
                f"target={target_period_code}, previous={previous_period_code}, "
                f"total_meters={total_meters}, index_rows={index_rows}, "
                f"copied_first_indexes={copied_first_indexes}, "
                f"missing_first_indexes={missing_first_indexes}, "
                f"calculation_rows={calculation_rows}"
            )

            cur.execute(
                """
                UPDATE automation_runs
                SET status = 'success',
                    finished_at = now(),
                    message = %s
                WHERE id = %s
                """,
                (message, run_id),
            )

        conn.commit()

    except Exception as exc:
        conn.rollback()
        raise exc

    finally:
        conn.close()

    print("Dönem açma işlemi tamamlandı.")
    print(f"Hedef dönem: {target_period_code}")
    print(f"Önceki dönem: {previous_period_code}")
    print(f"Toplam aktif sayaç: {total_meters}")
    print(f"Dönem endeks kayıtları: {index_rows}")
    print(f"Devreden ilk endeks sayısı: {copied_first_indexes}")
    print(f"İlk endeksi eksik sayaç: {missing_first_indexes}")
    print(f"Hesap kayıtları: {calculation_rows}")


if __name__ == "__main__":
    main()
