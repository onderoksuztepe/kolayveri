import argparse
import calendar
import os
import psycopg2
import psycopg2.extras
from datetime import date, datetime


def db():
    password = os.environ.get("KOLAYVERI_DB_PASSWORD")
    if not password:
        raise SystemExit("KOLAYVERI_DB_PASSWORD env yok. Önce .env_portal source edilmeli.")

    return psycopg2.connect(
        host="127.0.0.1",
        dbname="kolayveri_db",
        user="kolayveri_user",
        password=password,
    )


def next_period_code(period_code):
    year, month = map(int, period_code.split("-"))
    if month == 12:
        return f"{year + 1}-01"
    return f"{year}-{month + 1:02d}"


def period_dates(period_code):
    year, month = map(int, period_code.split("-"))
    start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = date(year, month, last_day)
    return start, end


def get_period(cur, period_code):
    cur.execute("""
        SELECT id, period_code, start_date, end_date, status
        FROM periods
        WHERE period_code = %s
    """, (period_code,))
    return cur.fetchone()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Kapanacak dönem. Örn: 2026-06")
    parser.add_argument("--target", default=None, help="Açılacak dönem. Boşsa sonraki ay.")
    parser.add_argument("--apply", action="store_true", help="Gerçekten uygula. Yoksa dry-run.")
    parser.add_argument("--overwrite", action="store_true", help="Hedef dönemde mevcut ilk endeksleri ez.")
    parser.add_argument("--force", action="store_true", help="Dönem bitmeden apply çalıştırmaya izin ver.")
    args = parser.parse_args()

    source_code = args.source
    target_code = args.target or next_period_code(source_code)

    target_start, target_end = period_dates(target_code)

    conn = db()
    conn.autocommit = False

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            source = get_period(cur, source_code)
            target = get_period(cur, target_code)

            if not source:
                raise SystemExit(f"Kaynak dönem bulunamadı: {source_code}")

            cur.execute("""
                SELECT
                    i.meter_id,
                    m.meter_serial,
                    m.meter_name,
                    i.last_active_index,
                    i.last_inductive_index,
                    i.last_capacitive_index
                FROM meter_period_indexes i
                JOIN meters m ON m.id = i.meter_id
                WHERE i.period_id = %s
                ORDER BY m.sort_order NULLS LAST, m.meter_serial
            """, (source["id"],))
            rows = cur.fetchall()

            total = len(rows)
            active_ready = sum(1 for r in rows if r["last_active_index"] is not None)
            inductive_ready = sum(1 for r in rows if r["last_inductive_index"] is not None)
            capacitive_ready = sum(1 for r in rows if r["last_capacitive_index"] is not None)
            reactive_ready = sum(
                1 for r in rows
                if r["last_inductive_index"] is not None and r["last_capacitive_index"] is not None
            )

            print("=== DÖNEM DEVİR KONTROLÜ ===")
            print(f"Kaynak dönem : {source_code} / status={source['status']}")
            print(f"Hedef dönem  : {target_code} / {'var' if target else 'yok, oluşturulacak'}")
            print(f"Hedef tarih  : {target_start} - {target_end}")
            print("")
            print(f"Toplam sayaç              : {total}")
            print(f"Aktif endeks devredecek   : {active_ready}")
            print(f"Endüktif endeks devredecek: {inductive_ready}")
            print(f"Kapasitif endeks devredecek: {capacitive_ready}")
            print(f"Tam reaktif devredecek    : {reactive_ready}")
            print(f"Aktif eksik               : {total - active_ready}")
            print(f"Tam reaktif eksik         : {total - reactive_ready}")
            print("")

            missing = [
                r for r in rows
                if r["last_active_index"] is None
                or r["last_inductive_index"] is None
                or r["last_capacitive_index"] is None
            ]

            if missing:
                print("İlk 30 eksik/kontrol sayaç:")
                for r in missing[:30]:
                    flags = []
                    if r["last_active_index"] is None:
                        flags.append("aktif")
                    if r["last_inductive_index"] is None:
                        flags.append("endüktif")
                    if r["last_capacitive_index"] is None:
                        flags.append("kapasitif")

                    print(f"- {r['meter_serial']} | {r['meter_name']} | eksik: {', '.join(flags)}")
                print("")

            if not args.apply:
                print("DRY_RUN: Değişiklik yapılmadı.")
                conn.rollback()
                return

            today = date.today()
            if today <= source["end_date"] and not args.force:
                print("GÜVENLİK KİLİDİ: Kaynak dönem henüz bitmemiş.")
                print(f"Bugün       : {today}")
                print(f"Dönem bitiş : {source['end_date']}")
                print("")
                print("Gerçekten erken kapatmak istiyorsan --force eklemelisin.")
                conn.rollback()
                return

            if not target:
                cur.execute("""
                    INSERT INTO periods (period_code, start_date, end_date, status)
                    VALUES (%s, %s, %s, 'open')
                    RETURNING id
                """, (target_code, target_start, target_end))
                target_id = cur.fetchone()["id"]
                print(f"Hedef dönem oluşturuldu: {target_code} id={target_id}")
            else:
                target_id = target["id"]
                cur.execute("""
                    UPDATE periods
                    SET status = 'open'
                    WHERE id = %s
                """, (target_id,))

            inserted_indexes = 0
            updated_indexes = 0
            inserted_calcs = 0

            for r in rows:
                cur.execute("""
                    SELECT id,
                           first_active_index,
                           first_inductive_index,
                           first_capacitive_index
                    FROM meter_period_indexes
                    WHERE meter_id = %s
                      AND period_id = %s
                """, (r["meter_id"], target_id))
                existing = cur.fetchone()

                if not existing:
                    cur.execute("""
                        INSERT INTO meter_period_indexes (
                            meter_id,
                            period_id,
                            first_active_index,
                            first_inductive_index,
                            first_capacitive_index,
                            first_source,
                            updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    """, (
                        r["meter_id"],
                        target_id,
                        r["last_active_index"],
                        r["last_inductive_index"],
                        r["last_capacitive_index"],
                        f"rollover:{source_code}",
                    ))
                    inserted_indexes += 1
                else:
                    if args.overwrite:
                        cur.execute("""
                            UPDATE meter_period_indexes
                            SET first_active_index = %s,
                                first_inductive_index = %s,
                                first_capacitive_index = %s,
                                first_source = %s,
                                updated_at = NOW()
                            WHERE id = %s
                        """, (
                            r["last_active_index"],
                            r["last_inductive_index"],
                            r["last_capacitive_index"],
                            f"rollover:{source_code}",
                            existing["id"],
                        ))
                        updated_indexes += 1
                    else:
                        cur.execute("""
                            UPDATE meter_period_indexes
                            SET first_active_index = COALESCE(first_active_index, %s),
                                first_inductive_index = COALESCE(first_inductive_index, %s),
                                first_capacitive_index = COALESCE(first_capacitive_index, %s),
                                first_source = COALESCE(first_source, %s),
                                updated_at = NOW()
                            WHERE id = %s
                        """, (
                            r["last_active_index"],
                            r["last_inductive_index"],
                            r["last_capacitive_index"],
                            f"rollover:{source_code}",
                            existing["id"],
                        ))
                        updated_indexes += 1

                cur.execute("""
                    SELECT id
                    FROM meter_period_calculations
                    WHERE meter_id = %s
                      AND period_id = %s
                """, (r["meter_id"], target_id))
                calc_existing = cur.fetchone()

                if not calc_existing:
                    cur.execute("""
                        INSERT INTO meter_period_calculations (
                            meter_id,
                            period_id,
                            calculation_status,
                            calculated_at
                        )
                        VALUES (%s, %s, 'pending', NOW())
                    """, (r["meter_id"], target_id))
                    inserted_calcs += 1

            cur.execute("""
                UPDATE periods
                SET status = 'closed'
                WHERE id = %s
            """, (source["id"],))

            conn.commit()

            print("APPLY tamamlandı.")
            print(f"Kapatılan dönem: {source_code}")
            print(f"Açılan dönem   : {target_code}")
            print(f"Index insert   : {inserted_indexes}")
            print(f"Index update   : {updated_indexes}")
            print(f"Calc insert    : {inserted_calcs}")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
