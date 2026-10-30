import sqlite3
import os
import json
import hashlib
import hmac
import secrets
import html
import time
import re
from datetime import datetime, timedelta
import random

DB_FILE = "brooder_data.db"
FIREBASE_URL = "https://angry-bird-56bc6-default-rtdb.asia-southeast1.firebasedatabase.app/"
FIREBASE_KEY_FILE = "firebase_key.json"

# ตั้งค่าระบบป้องกันการสุ่มรหัสผ่าน (Brute-Force Protection ตามมาตรฐาน OWASP)
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 60
_failed_login_tracker = {}  # {identifier: {"count": int, "locked_until": float}}

def sanitize_input(text: str, max_len: int = 120) -> str:
    """กรองและทำความสะอาดข้อความเพื่อป้องกัน Cross-Site Scripting (XSS) และโค้ดแปลกปลอม"""
    if not text:
        return ""
    cleaned = str(text).strip()
    # ลบแท็ก <script> และแอตทริบิวต์อันตรายออกทั้งหมด
    cleaned = re.sub(r"<\s*script[^>]*>.*?<\s*/\s*script\s*>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(r"javascript\s*:", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"on\w+\s*=", "", cleaned, flags=re.IGNORECASE)
    return cleaned[:max_len]

def _get_firebase_accounts_ref():
    """เชื่อมต่อ Firebase Realtime Database (Online Cloud) สำหรับเก็บข้อมูลบัญชีออนไลน์"""
    try:
        import firebase_admin
        from firebase_admin import credentials, db
        if not firebase_admin._apps:
            if os.path.exists(FIREBASE_KEY_FILE):
                cred = credentials.Certificate(FIREBASE_KEY_FILE)
                firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_URL})
            else:
                try:
                    import streamlit as st
                    if "firebase_key" in st.secrets:
                        key_dict = dict(st.secrets["firebase_key"])
                        if "private_key" in key_dict and isinstance(key_dict["private_key"], str):
                            key_dict["private_key"] = key_dict["private_key"].replace("\\n", "\n")
                        cred = credentials.Certificate(key_dict)
                        firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_URL})
                    else:
                        return None
                except BaseException:
                    return None
        return db.reference("user_accounts")
    except BaseException:
        return None

def _safe_fb_key(username: str) -> str:
    """แปลงชื่อผู้ใช้ให้เป็นคีย์ที่ปลอดภัยสำหรับ Firebase"""
    return re.sub(r'[.#$\[\]/]', '_', username.strip().lower())

def sync_account_to_cloud(username, password_hash, display_name, email, preferences_dict, created_at=None):
    """อัปโหลดข้อมูลบัญชีและการตั้งค่าขึ้น Firebase Cloud อัตโนมัติ"""
    ref = _get_firebase_accounts_ref()
    if ref is None:
        return False
    try:
        uname = username.strip().lower()
        key = _safe_fb_key(uname)
        payload = {
            "username": uname,
            "password_hash": password_hash,
            "display_name": display_name,
            "email": email.strip(),
            "preferences": preferences_dict if isinstance(preferences_dict, dict) else {},
            "created_at": created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        ref.child(key).set(payload)
        return True
    except Exception:
        return False

def sync_all_accounts_with_cloud():
    """ซิงค์ข้อมูลบัญชีแบบ 2 ทาง (ดึงจาก Firebase Cloud ลง SQLite และอัปโหลดจาก SQLite ขึ้น Cloud)"""
    init_db()
    ref = _get_firebase_accounts_ref()
    if ref is None:
        return False
    try:
        cloud_data = ref.get() or {}
        conn = get_connection()
        cursor = conn.cursor()

        # 1. ดึงบัญชีจาก Cloud ลงมาเก็บใน SQLite (กรณีสมัครจากเครื่องอื่น)
        if isinstance(cloud_data, dict):
            for _, acc in cloud_data.items():
                if not isinstance(acc, dict) or not acc.get("username"):
                    continue
                c_uname = acc["username"].strip().lower()
                cursor.execute("SELECT username FROM user_accounts WHERE LOWER(username) = ?", (c_uname,))
                if not cursor.fetchone():
                    prefs_str = json.dumps(acc.get("preferences", {}), ensure_ascii=False)
                    cursor.execute("""
                    INSERT INTO user_accounts (username, password_hash, display_name, email, preferences_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        c_uname,
                        acc.get("password_hash", ""),
                        acc.get("display_name", c_uname),
                        acc.get("email", ""),
                        prefs_str,
                        acc.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    ))
        conn.commit()

        # 2. อัปโหลดบัญชีที่มีในเครื่องแต่ยังไม่มีบน Cloud ขึ้นไปบน Firebase
        cursor.execute("SELECT username, password_hash, display_name, email, preferences_json, created_at FROM user_accounts")
        local_rows = cursor.fetchall()
        conn.close()

        for row in local_rows:
            key = _safe_fb_key(row["username"])
            if not isinstance(cloud_data, dict) or key not in cloud_data:
                try:
                    p_dict = json.loads(row["preferences_json"] or "{}")
                except Exception:
                    p_dict = {}
                sync_account_to_cloud(
                    row["username"],
                    row["password_hash"],
                    row["display_name"],
                    row["email"],
                    p_dict,
                    row["created_at"]
                )
        return True
    except Exception:
        return False

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def _hash_password(password: str, salt: str = None) -> str:
    """เข้ารหัสรหัสผ่านด้วย PBKDF2-HMAC-SHA256 (100,000 รอบ) พร้อม Random Salt ป้องกัน Rainbow Table"""
    if salt is None:
        salt = secrets.token_hex(16)
    iterations = 100000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"

def _verify_password(password: str, stored_hash: str) -> bool:
    """ตรวจสอบรหัสผ่านด้วย hmac.compare_digest (ป้องกัน Timing Attack) รองรับทั้ง PBKDF2 และ SHA-256 เดิม"""
    if not stored_hash or not password:
        return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            parts = stored_hash.split("$")
            iterations = int(parts[1])
            salt = parts[2]
            expected_hex = parts[3]
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
            return hmac.compare_digest(dk.hex(), expected_hex)
        except Exception:
            return False
    else:
        # รองรับบัญชีเดิมที่เข้ารหัสด้วย SHA-256 และจะอัปเกรดเป็น PBKDF2 อัตโนมัติเมื่อล็อกอินสำเร็จ
        legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(legacy_hash, stored_hash)

def init_db():
    """สร้างตารางเก็บสถิติเซนเซอร์ และตารางบัญชีผู้ใช้แยกแต่ละบัญชี"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sensor_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        temp REAL NOT NULL,
        hum REAL NOT NULL,
        nh3 REAL NOT NULL,
        weight REAL NOT NULL,
        heater_power INTEGER DEFAULT 0,
        fan_speed INTEGER DEFAULT 0,
        bird_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'Normal'
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sensor_time ON sensor_logs(timestamp)")

    # ตารางบัญชีผู้ใช้งาน (เก็บข้อมูลและการเชื่อมต่อแยกแต่ละบัญชี)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL,
        email TEXT DEFAULT '',
        preferences_json TEXT DEFAULT '{}',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def register_user(username, password, display_name="", email="", initial_prefs=None):
    """สมัครสมาชิกบัญชีใหม่ พร้อมกรอง XSS และเข้ารหัส PBKDF2-SHA256 ลง SQLite & Firebase Cloud"""
    init_db()
    uname = sanitize_input(username, 50).lower()
    email_clean = sanitize_input(email, 100)
    if not uname or not password:
        return False, "กรุณากรอกชื่อผู้ใช้และรหัสผ่านให้ครบถ้วน"
    if len(password) < 4:
        return False, "รหัสผ่านต้องมีความยาวอย่างน้อย 4 ตัวอักษร"

    disp = sanitize_input(display_name, 80) if display_name.strip() else sanitize_input(username, 80)
    pw_hash = _hash_password(password)
    prefs_dict = initial_prefs or {}
    prefs_str = json.dumps(prefs_dict, ensure_ascii=False)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        # ตรวจสอบว่าอีเมลซ้ำหรือไม่ (Parameterized Query ป้องกัน SQL Injection 100%)
        if email_clean:
            cursor.execute("SELECT username FROM user_accounts WHERE LOWER(email) = ?", (email_clean.lower(),))
            existing_email_row = cursor.fetchone()
            if existing_email_row:
                return False, f"อีเมล '{email_clean}' มีบัญชีในระบบแล้ว (ชื่อผู้ใช้: {existing_email_row['username']}) สามารถใช้ล็อกอินได้เลยครับ"

        cursor.execute("""
        INSERT INTO user_accounts (username, password_hash, display_name, email, preferences_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (uname, pw_hash, disp, email_clean, prefs_str, now_str))
        conn.commit()

        # ซิงค์ขึ้น Firebase Cloud ออนไลน์ทันที
        sync_account_to_cloud(uname, pw_hash, disp, email_clean, prefs_dict, now_str)
        return True, f"สมัครสมาชิกบัญชี '{disp}' และเข้ารหัสความปลอดภัยออนไลน์สำเร็จแล้ว!"
    except sqlite3.IntegrityError:
        return False, f"ชื่อผู้ใช้ '{uname}' มีในระบบแล้ว กรุณาใช้ชื่ออื่น หรือเข้าสู่ระบบด้วยชื่อนี้ได้เลยครับ"
    except Exception as e:
        return False, f"เกิดข้อผิดพลาด: {str(e)}"
    finally:
        conn.close()

def authenticate_user(identifier, password):
    """ตรวจสอบการเข้าสู่ระบบ พร้อมระบบป้องกันการสุ่มรหัส (Brute-Force Rate Limiting) และอัปเกรด PBKDF2"""
    init_db()
    ident = sanitize_input(identifier, 100).lower()
    now_ts = time.time()

    # 1. ตรวจสอบว่าถูกระงับชั่วคราวจาก Brute-Force Protection หรือไม่
    tracker = _failed_login_tracker.get(ident, {"count": 0, "locked_until": 0.0})
    if tracker["locked_until"] > now_ts:
        wait_sec = int(tracker["locked_until"] - now_ts) + 1
        return False, f"🛡️ ระบบป้องกันการเจาะรหัส (Brute-Force Protection): กรอกผิดเกิน {MAX_LOGIN_ATTEMPTS} ครั้ง กรุณารอ {wait_sec} วินาทีแล้วลองใหม่ครับ", None

    def _find_account(conn_obj):
        cur = conn_obj.cursor()
        cur.execute("""
        SELECT username, password_hash, display_name, email, preferences_json, created_at
        FROM user_accounts
        WHERE LOWER(username) = ? OR LOWER(email) = ? OR LOWER(display_name) = ?
        """, (ident, ident, ident))
        return cur.fetchone()

    conn = get_connection()
    row = _find_account(conn)

    # หากไม่พบในเครื่อง ให้ลองดึงข้อมูลล่าสุดจาก Firebase Cloud ออนไลน์แล้วค้นหาอีกครั้ง
    if not row:
        conn.close()
        sync_all_accounts_with_cloud()
        conn = get_connection()
        row = _find_account(conn)

    if not row:
        conn.close()
        return False, f"ไม่พบบัญชี '{ident}' ในระบบ (ตรวจสอบชื่อผู้ใช้/อีเมล หรือกดสมัครบัญชีใหม่ด้านบนครับ)", None

    try:
        prefs = json.loads(row["preferences_json"] or "{}")
    except Exception:
        prefs = {}

    # 2. ตรวจสอบรหัสผ่านด้วย Timing-Safe Comparison
    if not _verify_password(password, row["password_hash"]):
        conn.close()
        tracker["count"] = tracker.get("count", 0) + 1
        if tracker["count"] >= MAX_LOGIN_ATTEMPTS:
            tracker["locked_until"] = now_ts + LOCKOUT_DURATION_SECONDS
            tracker["count"] = 0
            _failed_login_tracker[ident] = tracker
            # ส่งแจ้งเตือนความปลอดภัยเข้า Telegram ของเจ้าของบัญชีทันที (ถ้าผูกไว้)
            try:
                tg_cid = str(prefs.get("telegram_chat_id", "")).strip()
                if tg_cid:
                    import telegram_notifier
                    telegram_notifier.send_telegram(
                        f"🛡️ <b>แจ้งเตือนความปลอดภัย NestX Security!</b>\n"
                        f"พบความพยายามสุ่มรหัสผ่านเข้าบัญชี <b>{row['display_name']}</b> ผิดติดต่อกัน {MAX_LOGIN_ATTEMPTS} ครั้ง!\n"
                        f"🔒 ระบบได้ทำการล็อคการเข้าสู่ระบบชั่วคราวเป็นเวลา {LOCKOUT_DURATION_SECONDS} วินาทีเรียบร้อยแล้ว",
                        chat_id=tg_cid
                    )
            except Exception:
                pass
            return False, f"🚨 กรอกรหัสผ่านผิดครบ {MAX_LOGIN_ATTEMPTS} ครั้ง! ระบบล็อคการเข้าสู่ระบบชั่วคราว {LOCKOUT_DURATION_SECONDS} วินาทีเพื่อความปลอดภัย", None
        else:
            _failed_login_tracker[ident] = tracker
            rem = MAX_LOGIN_ATTEMPTS - tracker["count"]
            return False, f"รหัสผ่านไม่ถูกต้อง กรุณาลองอีกครั้ง (เหลือโอกาสอีก {rem} ครั้งก่อนระบบล็อคชั่วคราว)", None

    # 3. ล็อกอินสำเร็จ -> รีเซ็ตตัวนับการกรอกผิด
    _failed_login_tracker.pop(ident, None)

    # หากยังเป็น Hash แบบเก่า (SHA-256) ให้ยกระดับเป็น PBKDF2-HMAC-SHA256 + Salt อัตโนมัติทันที
    final_pw_hash = row["password_hash"]
    if not final_pw_hash.startswith("pbkdf2_sha256$"):
        final_pw_hash = _hash_password(password)
        try:
            cur = conn.cursor()
            cur.execute("UPDATE user_accounts SET password_hash = ? WHERE username = ?", (final_pw_hash, row["username"]))
            conn.commit()
        except Exception:
            pass
    conn.close()

    # ซิงค์ข้อมูลขึ้น Cloud เพื่อให้มั่นใจว่าข้อมูลออนไลน์เป็นปัจจุบันเสมอ
    sync_account_to_cloud(
        row["username"],
        final_pw_hash,
        row["display_name"],
        row["email"],
        prefs,
        row["created_at"]
    )

    user_info = {
        "username": row["username"],
        "display_name": row["display_name"],
        "email": row["email"],
        "created_at": row["created_at"],
        "preferences": prefs
    }
    return True, f"ยินดีต้อนรับกลับมา คุณ {row['display_name']}!", user_info

# เก็บรหัสลับ OTP สำหรับกู้คืนรหัสผ่านทาง Telegram (อายุรหัส 5 นาที)
_password_reset_otps = {}  # {username: {"otp": str, "expires_at": float, "attempts": int, "tg_cid": str}}
OTP_EXPIRE_SECONDS = 300

def request_password_reset_otp(identifier):
    """ค้นหาบัญชีและส่งรหัสลับ OTP 6 หลักไปยัง Telegram ที่ผูกไว้กับบัญชีนั้น"""
    init_db()
    ident = sanitize_input(identifier, 100).lower()
    if not ident:
        return False, "กรุณากรอกชื่อผู้ใช้ หรือ อีเมล ของบัญชีที่ต้องการกู้รหัสผ่านครับ", None

    def _find_acc(conn_obj):
        cur = conn_obj.cursor()
        cur.execute("""
        SELECT username, password_hash, display_name, email, preferences_json, created_at
        FROM user_accounts
        WHERE LOWER(username) = ? OR LOWER(email) = ? OR LOWER(display_name) = ?
        """, (ident, ident, ident))
        return cur.fetchone()

    conn = get_connection()
    row = _find_acc(conn)
    if not row:
        conn.close()
        sync_all_accounts_with_cloud()
        conn = get_connection()
        row = _find_acc(conn)
    conn.close()

    if not row:
        return False, f"ไม่พบบัญชี '{ident}' ในระบบ กรุณาตรวจสอบชื่อผู้ใช้หรืออีเมลอีกครั้งครับ", None

    try:
        prefs = json.loads(row["preferences_json"] or "{}")
    except Exception:
        prefs = {}

    tg_cid = str(prefs.get("telegram_chat_id", "")).strip()
    tg_name = str(prefs.get("telegram_username", "")).strip() or "Telegram ของคุณ"

    if not tg_cid:
        return (
            False,
            f"บัญชี '{row['display_name']}' (@{row['username']}) ยังไม่ได้เชื่อมต่อกับ Telegram จึงไม่สามารถส่งรหัสลับ OTP ทาง Telegram ได้ครับ",
            None
        )

    # สร้างรหัสลับ OTP 6 หลักด้วยโมดูล secrets (Cryptographically Secure)
    otp_code = f"{secrets.randbelow(900000) + 100000}"
    _password_reset_otps[row["username"]] = {
        "otp": otp_code,
        "expires_at": time.time() + OTP_EXPIRE_SECONDS,
        "attempts": 0,
        "tg_cid": tg_cid
    }

    # ส่งรหัสลับเข้า Telegram ของเจ้าของบัญชีทันที
    try:
        import telegram_notifier
        msg_html = (
            f"🔐 <b>รหัสลับกู้คืนรหัสผ่าน NestX Smart Brooder</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>บัญชีผู้ใช้:</b> {row['display_name']} (@{row['username']})\n"
            f"🔑 <b>รหัสลับ OTP 6 หลักของคุณคือ:</b>\n\n"
            f"👉 <code>{otp_code}</code> 👈\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <i>รหัสนี้มีอายุใช้งาน 5 นาที กรุณานำไปกรอกในหน้าเว็บเพื่อตั้งรหัสผ่านใหม่ (ห้ามบอกรหัสนี้กับผู้อื่น)</i>"
        )
        ok, err_msg = telegram_notifier.send_telegram(msg_html, chat_id=tg_cid)
        if not ok:
            return False, f"ไม่สามารถส่งข้อความเข้า Telegram ได้ ({err_msg})", None
    except Exception as e:
        return False, f"เกิดข้อผิดพลาดในการส่ง Telegram: {str(e)}", None

    return (
        True,
        f"✈️ ส่งรหัสลับ OTP 6 หลักไปยัง Telegram ({tg_name}) เรียบร้อยแล้ว! กรุณาเปิดดูรหัสในแอป Telegram",
        {"username": row["username"], "display_name": row["display_name"], "tg_name": tg_name}
    )

def verify_otp_and_reset_password(identifier, otp_input, new_password):
    """ตรวจสอบรหัสลับ OTP 6 หลัก และตั้งรหัสผ่านใหม่พร้อมซิงค์ขึ้น Firebase Cloud"""
    init_db()
    ident = sanitize_input(identifier, 100).lower()
    otp_clean = str(otp_input).strip()

    if not ident or not otp_clean or not new_password:
        return False, "กรุณากรอกรหัส OTP และรหัสผ่านใหม่ให้ครบถ้วนครับ", None
    if len(new_password) < 4:
        return False, "รหัสผ่านใหม่ต้องมีความยาวอย่างน้อย 4 ตัวอักษรครับ", None

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    SELECT username, password_hash, display_name, email, preferences_json, created_at
    FROM user_accounts
    WHERE LOWER(username) = ? OR LOWER(email) = ? OR LOWER(display_name) = ?
    """, (ident, ident, ident))
    row = cur.fetchone()

    if not row:
        conn.close()
        return False, "ไม่พบบัญชีผู้ใช้นี้ในระบบครับ", None

    uname = row["username"]
    otp_record = _password_reset_otps.get(uname)
    if not otp_record:
        conn.close()
        return False, "ยังไม่ได้ขอรหัส OTP หรือรหัสหมดอายุแล้ว กรุณากดขอรหัส OTP ใหม่อีกครั้งครับ", None

    if time.time() > otp_record["expires_at"]:
        _password_reset_otps.pop(uname, None)
        conn.close()
        return False, "⏰ รหัส OTP นี้หมดอายุแล้ว (เกิน 5 นาที) กรุณากดส่งรหัส OTP ใหม่ครับ", None

    if not hmac.compare_digest(otp_clean, otp_record["otp"]):
        otp_record["attempts"] += 1
        if otp_record["attempts"] >= 5:
            _password_reset_otps.pop(uname, None)
            conn.close()
            return False, "🚨 กรอกรหัส OTP ผิดเกิน 5 ครั้ง ระบบได้ยกเลิกรหัสชุดนี้แล้ว กรุณากดขอรหัสใหม่ครับ", None
        rem = 5 - otp_record["attempts"]
        conn.close()
        return False, f"❌ รหัสลับ OTP ไม่ถูกต้อง กรุณาตรวจสอบใน Telegram อีกครั้ง (เหลือโอกาส {rem} ครั้ง)", None

    # รหัส OTP ถูกต้อง -> เข้ารหัสรหัสผ่านใหม่ด้วย PBKDF2-HMAC-SHA256 + Random Salt
    new_pw_hash = _hash_password(new_password)
    cur.execute("UPDATE user_accounts SET password_hash = ? WHERE username = ?", (new_pw_hash, uname))
    conn.commit()
    conn.close()

    # ลบรหัส OTP ที่ใช้แล้ว และปลดล็อค Brute-Force (ถ้ามี)
    tg_cid = otp_record.get("tg_cid", "")
    _password_reset_otps.pop(uname, None)
    _failed_login_tracker.pop(uname, None)
    _failed_login_tracker.pop(ident, None)

    try:
        prefs = json.loads(row["preferences_json"] or "{}")
    except Exception:
        prefs = {}

    # ซิงค์รหัสผ่านใหม่ขึ้น Firebase Cloud ออนไลน์ทันที
    sync_account_to_cloud(uname, new_pw_hash, row["display_name"], row["email"], prefs, row["created_at"])

    # แจ้งเตือนยืนยันเข้า Telegram ว่าเปลี่ยนรหัสสำเร็จแล้ว
    if tg_cid:
        try:
            import telegram_notifier
            telegram_notifier.send_telegram(
                f"✅ <b>กู้คืนและเปลี่ยนรหัสผ่านสำเร็จ!</b>\n"
                f"บัญชี <b>{row['display_name']} (@{uname})</b> ได้ตั้งรหัสผ่านใหม่ผ่านระบบ OTP เรียบร้อยแล้วครับ 🔐",
                chat_id=tg_cid
            )
        except Exception:
            pass

    user_info = {
        "username": uname,
        "display_name": row["display_name"],
        "email": row["email"],
        "created_at": row["created_at"],
        "preferences": prefs
    }
    return True, f"🎉 กู้คืนและเปลี่ยนรหัสผ่านของ '{row['display_name']}' สำเร็จแล้ว!", user_info

def save_user_account_prefs(username, prefs_dict):
    """บันทึกข้อมูลการตั้งค่าและการเชื่อมต่อ (Telegram/LINE/Discord/ฯลฯ) ลงทั้ง SQLite และ Firebase Cloud"""
    if not username:
        return False
    init_db()
    uname = username.strip().lower()
    prefs_str = json.dumps(prefs_dict, ensure_ascii=False)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE user_accounts
    SET preferences_json = ?,
        display_name = COALESCE(NULLIF(?, ''), display_name),
        email = COALESCE(NULLIF(?, ''), email)
    WHERE LOWER(username) = ? OR LOWER(email) = ?
    """, (
        prefs_str,
        prefs_dict.get("account_display_name", ""),
        prefs_dict.get("email_address", ""),
        uname,
        uname
    ))
    conn.commit()

    # ดึงข้อมูลแถวล่าสุดเพื่อซิงค์ขึ้น Firebase Cloud ออนไลน์
    cursor.execute("""
    SELECT username, password_hash, display_name, email, created_at
    FROM user_accounts WHERE LOWER(username) = ? OR LOWER(email) = ?
    """, (uname, uname))
    row = cursor.fetchone()
    conn.close()

    if row:
        sync_account_to_cloud(
            row["username"],
            row["password_hash"],
            row["display_name"],
            row["email"],
            prefs_dict,
            row["created_at"]
        )
    return True

def get_user_account_prefs(username):
    """ดึงข้อมูลการตั้งค่าเฉพาะของบัญชีที่ระบุ (ค้นหาได้ทั้งจาก Username หรือ Email)"""
    if not username:
        return None
    init_db()
    uname = username.strip().lower()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT username, display_name, email, preferences_json, created_at
    FROM user_accounts
    WHERE LOWER(username) = ? OR LOWER(email) = ?
    """, (uname, uname))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    try:
        prefs = json.loads(row["preferences_json"] or "{}")
    except Exception:
        prefs = {}
    prefs["account_username"] = row["username"]
    prefs["account_display_name"] = row["display_name"]
    prefs["account_created_at"] = row["created_at"]
    if row["email"] and not prefs.get("email_address"):
        prefs["email_address"] = row["email"]
    return prefs

def list_all_accounts():
    """ดึงรายชื่อบัญชีทั้งหมดในระบบ (สำหรับแสดงรายการบัญชีที่เคยสมัครไว้)"""
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT username, display_name, email, created_at FROM user_accounts ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def log_sensor_reading(temp, hum, nh3, weight, heater_power=0, fan_speed=0, bird_count=0, status='Normal', timestamp=None):
    """บันทึกข้อมูลเซนเซอร์ลงใน SQLite DB"""
    init_db()
    conn = get_connection()
    cursor = conn.cursor()
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    INSERT INTO sensor_logs (timestamp, temp, hum, nh3, weight, heater_power, fan_speed, bird_count, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (timestamp, float(temp), float(hum), float(nh3), float(weight), int(heater_power), int(fan_speed), int(bird_count), str(status)))
    conn.commit()
    conn.close()

def get_history(hours=24):
    """ดึงข้อมูลย้อนหลังตามจำนวนชั่วโมงที่กำหนด"""
    init_db()
    seed_mock_history_if_empty()
    conn = get_connection()
    since_time = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.cursor()
    cursor.execute("""
    SELECT timestamp, temp, hum, nh3, weight, heater_power, fan_speed, bird_count, status
    FROM sensor_logs
    WHERE timestamp >= ?
    ORDER BY timestamp ASC
    """, (since_time,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_daily_summary(target_date=None):
    """คำนวณสถิติประจำวันสำหรับออกรายงาน Telegram"""
    init_db()
    seed_mock_history_if_empty()
    if target_date is None:
        target_date = datetime.now().strftime("%Y-%m-%d")
    
    start_str = f"{target_date} 00:00:00"
    end_str = f"{target_date} 23:59:59"

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT 
        COUNT(*) as total_samples,
        AVG(temp) as avg_temp,
        MIN(temp) as min_temp,
        MAX(temp) as max_temp,
        AVG(hum) as avg_hum,
        MIN(hum) as min_hum,
        MAX(hum) as max_hum,
        AVG(nh3) as avg_nh3,
        MAX(nh3) as max_nh3,
        MIN(weight) as min_weight,
        MAX(weight) as max_weight
    FROM sensor_logs
    WHERE timestamp >= ? AND timestamp <= ?
    """, (start_str, end_str))
    stat = dict(cursor.fetchone())

    # ดึงน้ำหนักต้นวันและท้ายวันเพื่อหาการเติบโต
    cursor.execute("""
    SELECT weight FROM sensor_logs 
    WHERE timestamp >= ? AND timestamp <= ?
    ORDER BY timestamp ASC LIMIT 1
    """, (start_str, end_str))
    first_row = cursor.fetchone()
    start_weight = first_row["weight"] if first_row else 0.0

    cursor.execute("""
    SELECT weight FROM sensor_logs 
    WHERE timestamp >= ? AND timestamp <= ?
    ORDER BY timestamp DESC LIMIT 1
    """, (start_str, end_str))
    last_row = cursor.fetchone()
    end_weight = last_row["weight"] if last_row else 0.0

    conn.close()

    stat["start_weight"] = round(start_weight, 1)
    stat["end_weight"] = round(end_weight, 1)
    stat["weight_gain"] = round(end_weight - start_weight, 1)
    stat["date"] = target_date
    return stat

def seed_mock_history_if_empty():
    """สร้างข้อมูลจำลองย้อนหลัง 7 วันเพื่อเป็นฐานสถิติหากเพิ่งเริ่มใช้งาน"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM sensor_logs")
    count = cursor.fetchone()["cnt"]
    if count == 0:
        base_time = datetime.now() - timedelta(days=7)
        base_weight = 68.0
        records = []
        for d in range(7 * 24):  # ทุกๆ 1 ชม. ใน 7 วัน
            cur_time = base_time + timedelta(hours=d)
            t_str = cur_time.strftime("%Y-%m-%d %H:%M:%S")
            temp = round(35.5 + random.uniform(-0.4, 0.4), 1)
            hum = round(57.0 + random.uniform(-3, 3), 1)
            nh3 = random.randint(4, 9)
            base_weight += random.uniform(0.04, 0.09)
            weight = round(base_weight, 1)
            records.append((t_str, temp, hum, nh3, weight, 35, 20, 2, 'Normal'))
        
        cursor.executemany("""
        INSERT INTO sensor_logs (timestamp, temp, hum, nh3, weight, heater_power, fan_speed, bird_count, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    seed_mock_history_if_empty()
    summary = get_daily_summary()
    print("Database Initialized successfully.")
    print("Sample Daily Summary:", summary)
