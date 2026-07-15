import os
import sys
import base64
import hashlib
import secrets
import getpass
import psycopg2


DB_CONFIG = {
    "host": "127.0.0.1",
    "dbname": "kolayveri_db",
    "user": "kolayveri_user",
    "password": os.getenv("KOLAYVERI_DB_PASSWORD", "Kv2026ChangeMe123"),
    "port": 5432,
}


def hash_password(password: str) -> str:
    iterations = 260000
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode()
    )


def main():
    username = input("Kullanıcı adı: ").strip()
    role = input("Rol admin/customer [customer]: ").strip() or "customer"

    if role not in ("admin", "customer"):
        print("Rol sadece admin veya customer olabilir.")
        sys.exit(1)

    site_id = None
    if role != "admin":
        site_id_text = input("site_id: ").strip()
        site_id = int(site_id_text)

    password = getpass.getpass("Şifre: ")
    password2 = getpass.getpass("Şifre tekrar: ")

    if password != password2:
        print("Şifreler eşleşmedi.")
        sys.exit(1)

    password_hash = hash_password(password)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO portal_users (username, password_hash, role, site_id, is_active)
                    VALUES (%s, %s, %s, %s, TRUE)
                    ON CONFLICT (username)
                    DO UPDATE SET
                        password_hash = EXCLUDED.password_hash,
                        role = EXCLUDED.role,
                        site_id = EXCLUDED.site_id,
                        is_active = TRUE;
                """, (username, password_hash, role, site_id))
        print("Kullanıcı oluşturuldu/güncellendi:", username)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
