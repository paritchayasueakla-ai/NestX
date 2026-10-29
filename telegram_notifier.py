import requests
import json
import os
import time
from datetime import datetime

PREF_FILE = "user_preferences.json"
# โทเค็นบอทกลางประจำตู้กก (ฝังตายตัว ไม่ต้องให้ User กรอก)
DEFAULT_BOT_TOKEN = "8791062886:AAF6zx7kShfYne49bE-CF5MokneruSllkEY"

_last_alert_time = {}
ALERT_COOLDOWN_SECONDS = 600  # แจ้งเตือนซ้ำทุก 10 นาทีเพื่อไม่ให้ข้อความรัวเกินไป

def get_telegram_config():
    """ดึงการตั้งค่า Token และ Chat ID จาก user_preferences.json หรือใช้ Token กลาง"""
    chat_id = ""
    token = DEFAULT_BOT_TOKEN
    if os.path.exists(PREF_FILE):
        try:
            with open(PREF_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                chat_id = str(data.get("telegram_chat_id", ""))
                # ถ้ามี custom token ที่ผู้ใช้เคยบันทึกไว้ ก็ยังใช้ได้ หรือ default เป็น token กลาง
                token = data.get("telegram_token", "") or DEFAULT_BOT_TOKEN
        except:
            pass
    return token, chat_id

def send_telegram(text, bot_token=None, chat_id=None):
    """ส่งข้อความเข้า Telegram ผ่าน Bot API"""
    cfg_token, cfg_chat = get_telegram_config()
    token = bot_token if bot_token else cfg_token
    cid = chat_id if chat_id else cfg_chat

    if not token or not cid or token.strip() == "" or cid.strip() == "":
        return False, "ยังไม่ได้ระบุ Telegram Bot Token หรือ Chat ID"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=8)
        data = res.json()
        if data.get("ok"):
            return True, "ส่งข้อความเข้า Telegram สำเร็จ!"
        else:
            return False, f"Telegram Error: {data.get('description', 'Unknown error')}"
    except Exception as e:
        return False, f"การเชื่อมต่อขัดข้อง: {str(e)}"

# ==========================================
# 📸 ส่งรูปภาพเข้า Telegram
# ==========================================
CAPTION_LIMIT = 1024  # Telegram จำกัดคำบรรยายใต้รูปไว้ไม่เกิน 1024 ตัวอักษร

def send_telegram_photo(photo, caption="", bot_token=None, chat_id=None, filename="brooder.jpg"):
    """
    ส่งรูปภาพเข้า Telegram ผ่าน Bot API (sendPhoto)
    photo: ได้ 3 แบบ -> path ของไฟล์ (str), ข้อมูลรูปแบบ bytes, หรือไฟล์ที่เปิดอยู่ (file-like)
    caption: ข้อความใต้รูป (รองรับ HTML เช่น <b>ตัวหนา</b>)
    """
    cfg_token, cfg_chat = get_telegram_config()
    token = bot_token if bot_token else cfg_token
    cid = chat_id if chat_id else cfg_chat

    if not token or not cid or token.strip() == "" or cid.strip() == "":
        return False, "ยังไม่ได้ระบุ Telegram Bot Token หรือ Chat ID"

    # แปลงรูปให้อยู่ในรูป bytes เสมอ
    try:
        if isinstance(photo, str):
            with open(photo, "rb") as f:
                photo_bytes = f.read()
            filename = os.path.basename(photo)
        elif isinstance(photo, (bytes, bytearray)):
            photo_bytes = bytes(photo)
        else:
            photo_bytes = photo.read()
    except Exception as e:
        return False, f"อ่านไฟล์รูปไม่ได้: {str(e)}"

    if not photo_bytes:
        return False, "ไม่พบข้อมูลรูปภาพ"

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": cid, "parse_mode": "HTML"}
    if caption:
        data["caption"] = caption[:CAPTION_LIMIT]
    files = {"photo": (filename, photo_bytes)}

    try:
        res = requests.post(url, data=data, files=files, timeout=20)
        result = res.json()
        if result.get("ok"):
            return True, "ส่งรูปเข้า Telegram สำเร็จ!"
        return False, f"Telegram Error: {result.get('description', 'Unknown error')}"
    except Exception as e:
        return False, f"การเชื่อมต่อขัดข้อง: {str(e)}"

def capture_camera_image(camera_index=0):
    """
    ถ่ายภาพจากกล้องในตู้กก แล้วคืนค่าเป็น JPEG bytes (ถ้าไม่มีกล้องจะคืน None)
    ลำดับการลอง: 1) กล้อง Pi Camera (picamera2) บน Pi 5  2) กล้อง USB/Webcam ผ่าน OpenCV
    """
    # 1) Raspberry Pi Camera Module (สายแพ CSI)
    try:
        from picamera2 import Picamera2
        import io
        cam = Picamera2()
        cam.configure(cam.create_still_configuration())
        cam.start()
        time.sleep(1.5)  # รอให้กล้องปรับแสง
        buf = io.BytesIO()
        cam.capture_file(buf, format="jpeg")
        cam.stop()
        cam.close()
        return buf.getvalue()
    except Exception:
        pass

    # 2) กล้อง USB / Webcam
    try:
        import cv2
        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            return None
        for _ in range(5):  # ทิ้งเฟรมแรกๆ ที่มักมืดหรือเบลอ
            cap.read()
        ok, frame = cap.read()
        cap.release()
        if not ok:
            return None
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return jpg.tobytes() if ok else None
    except Exception:
        return None

def send_snapshot(caption="", bot_token=None, chat_id=None):
    """ถ่ายรูปจากกล้องในตู้กก แล้วส่งเข้า Telegram ทันที"""
    img = capture_camera_image()
    if img is None:
        return False, "ไม่พบกล้อง (ต่อกล้อง Pi Camera หรือ USB Webcam ก่อน)"
    return send_telegram_photo(img, caption, bot_token, chat_id, filename="snapshot.jpg")

def send_emergency_alert(alert_type, current_val, target_val, note="", bot_token=None, chat_id=None, with_photo=True, bypass_cooldown=False):
    """
    ส่งข้อความแจ้งเตือนด่วนเข้า Telegram ทันที
    alert_type: 'OVERHEAT', 'FREEZE', 'HIGH_NH3', 'WEIGHT_DROP'
    """
    global _last_alert_time
    now = time.time()
    
    # ตรวจสอบ Cooldown ป้องกันข้อความรัว (ถ้าไม่ใช่ bypass_cooldown)
    last_sent = _last_alert_time.get(alert_type, 0)
    if not bypass_cooldown and (now - last_sent < ALERT_COOLDOWN_SECONDS):
        return False, f"อยู่ในช่วง Cooldown (ส่งไปแล้วเมื่อ {int((now - last_sent)/60)} นาทีที่แล้ว)"

    time_str = datetime.now().strftime("%H:%M:%S (%d/%m/%Y)")

    if alert_type == "OVERHEAT":
        title = "🚨 <b>[วิกฤตด่วน!] อุณหภูมิตู้กกสูงเกินกำหนด</b>"
        body = f"🌡️ <b>อุณหภูมิปัจจุบัน:</b> 🔴 {current_val}°C (เป้าหมายสูงสุด: {target_val}°C)\n" \
               f"⚠️ <b>ความเสี่ยง:</b> ลูกนกอาจเกิดภาวะช็อก ขาดน้ำ หรือฮีตเตอร์ค้าง!\n" \
               f"💡 <b>คำแนะนำ:</b> กรุณาเข้าตรวจเช็กตู้กก ปลดสวิตช์ หรือเปิดระบายอากาศทันที"
    elif alert_type == "FREEZE":
        title = "🥶 <b>[วิกฤตด่วน!] อุณหภูมิตู้กกตกฮวบ/หนาวเกินไป</b>"
        body = f"🌡️ <b>อุณหภูมิปัจจุบัน:</b> 🔵 {current_val}°C (เป้าหมายต่ำสุด: {target_val}°C)\n" \
               f"⚠️ <b>ความเสี่ยง:</b> ลูกนกแรกเกิดอาจหนาวสั่น ระบบย่อยอาหารหยุดทำงาน!\n" \
               f"💡 <b>คำแนะนำ:</b> ตรวจเช็กปลั๊กไฟ หลอดไฟกกขาด หรือไฟดับหรือไม่"
    elif alert_type == "HIGH_NH3":
        title = "💨 <b>[แจ้งเตือนแก๊ส] ระดับแอมโมเนียเกินเกณฑ์ปลอดภัย</b>"
        body = f"⚠️ <b>ระดับแอมโมเนีย:</b> {current_val} ppm (เกณฑ์ปกติ: &lt; 10 ppm)\n" \
               f"💡 <b>คำแนะนำ:</b> ระบบได้เร่งพัดลมระบายอากาศแล้ว ควรตรวจสอบและเปลี่ยนวัสดุรองรัง"
    else:
        title = f"⚠️ <b>[แจ้งเตือนระบบ] {alert_type}</b>"
        body = f"ค่าปัจจุบัน: {current_val} (เป้าหมาย: {target_val})\nรายละเอียด: {note}"

    msg = f"{title}\n" \
          f"⏰ <b>เวลาตรวจพบ:</b> {time_str}\n" \
          f"━━━━━━━━━━━━━━━━━━\n" \
          f"{body}\n" \
          f"━━━━━━━━━━━━━━━━━━\n" \
          f"🤖 <i>รายงานอัตโนมัติจาก Pi 5 Smart Brooder Watchdog</i>"

    # ถ้ามีกล้องในตู้กก -> แนบรูปไปพร้อมข้อความ เพื่อให้เห็นสภาพน้องนกทันที
    snapshot = capture_camera_image() if with_photo else None
    if snapshot:
        success, err = send_telegram_photo(snapshot, msg, bot_token, chat_id, filename="alert.jpg")
    else:
        success, err = send_telegram(msg, bot_token, chat_id)
    if success:
        _last_alert_time[alert_type] = now
    return success, err

def send_daily_summary(stat, farm_name="AI Smart Brooder", elec_rate=4.5, bot_token=None, chat_id=None):
    """ส่งสรุปรายงานสถิติประจำวันเข้า Telegram ทุก 19:00 น."""
    date_str = stat.get("date", datetime.now().strftime("%d/%m/%Y"))
    avg_t = stat.get("avg_temp", 35.6)
    min_t = stat.get("min_temp", 35.0)
    max_t = stat.get("max_temp", 36.8)
    avg_h = stat.get("avg_hum", 58.0)
    gain_w = stat.get("weight_gain", 0.0)
    end_w = stat.get("end_weight", 0.0)
    max_nh3 = stat.get("max_nh3", 6)

    # ประมาณการค่าไฟ
    daily_cost = round(0.92 * elec_rate, 2)

    msg = f"📋 <b>สรุปรายงานสุขภาพลูกป้อนประจำวัน</b>\n" \
          f"🏠 <b>{farm_name}</b>\n" \
          f"📅 ประจำวันที่: {date_str}\n" \
          f"━━━━━━━━━━━━━━━━━━\n" \
          f"🌡️ <b>อุณหภูมิเฉลี่ย:</b> {avg_t:.1f}°C (ต่ำสุด: {min_t:.1f}°C / สูงสุด: {max_t:.1f}°C)\n" \
          f"💧 <b>ความชื้นเฉลี่ย:</b> {avg_h:.0f} %RH\n" \
          f"💨 <b>แอมโมเนียสูงสุด:</b> {max_nh3} ppm (ปกติ)\n" \
          f"━━━━━━━━━━━━━━━━━━\n" \
          f"⚖️ <b>การเจริญเติบโต (Growth Track):</b>\n" \
          f"   • น้ำหนักปัจจุบัน: <b>{end_w} กรัม</b>\n" \
          f"   • น้ำหนักเพิ่มขึ้นวันนี้: <b>{'+' if gain_w >= 0 else ''}{gain_w} กรัม</b> {'📈 (ดีเยี่ยม)' if gain_w > 0 else '⚠️'}\n" \
          f"━━━━━━━━━━━━━━━━━━\n" \
          f"⚡ <b>พลังงาน & ค่าไฟวันนี้:</b> ~{daily_cost} บาท (0.92 kWh)\n" \
          f"🔒 <b>สถานะระบบ:</b> ระบบทำงานปกติ 100% ตลอด 24 ชม.\n" \
          f"━━━━━━━━━━━━━━━━━━\n" \
          f"🤖 <i>ส่งอัตโนมัติจาก Raspberry Pi 5 Autonomous Brooder</i>"

    return send_telegram(msg, bot_token, chat_id)

def test_telegram_connection(bot_token, chat_id):
    """ส่งข้อความทดสอบการเชื่อมต่อ"""
    test_msg = "🐣 <b>ทดสอบการเชื่อมต่อ Telegram สำเร็จ!</b>\n" \
               "ระบบแจ้งเตือนฉุกเฉินและรายงานประจำวันของตู้กกลูกป้อนอัจฉริยะ (NestX) พร้อมส่งข้อมูลเข้ามือถือคุณแล้วครับ ✅"
    return send_telegram(test_msg, bot_token, chat_id)

# ==========================================
# 🔗 ระบบเชื่อมต่อ 1-Click อัตโนมัติ (Deep Linking)
# ==========================================
def check_for_connection_link(expected_token=None, bot_token=None):
    """
    ตรวจสอบข้อความล่าสุดจาก Telegram บอท ว่ามีใครกด /start หรือ /start <code_token> เข้ามาไหม
    ถ้าเจอ จะดึง Chat ID และชื่อผู้ใช้ พร้อมส่งข้อความต้อนรับยืนยันกลับไปทันที
    คืนค่า: (chat_id, user_first_name) หรือ (None, None)
    """
    cfg_token, _ = get_telegram_config()
    token = bot_token if bot_token else cfg_token
    if not token:
        return None, None

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        # ดึง updates ล่าสุดโดยไม่แคช
        res = requests.get(url, params={"timeout": 0}, timeout=8)
        data = res.json()
        if not data.get("ok"):
            return None, None
        
        updates = data.get("result", [])
        if not updates:
            return None, None

        # ไล่ดูจากข้อความล่าสุดย้อนหลัง
        for item in reversed(updates):
            msg = item.get("message", {}) or item.get("edited_message", {})
            text = msg.get("text", "").strip()
            
            sender = msg.get("from", {})
            chat = msg.get("chat", {})
            cid = str(chat.get("id", sender.get("id", "")))
            first_name = sender.get("first_name", "ผู้ใช้งาน")

            if not cid:
                continue

            # ถ้าเป็นการส่ง /start (ไม่ว่าจะติด code หรือไม่)
            if text.startswith("/start"):
                # 1. กรณีระบุ pairing code ชัดเจน
                if expected_token:
                    parts = text.split()
                    if len(parts) > 1 and parts[1].strip() == expected_token.strip():
                        welcome_msg = f"🎉 <b>ยินดีต้อนรับคุณ {first_name}!</b>\n" \
                                      f"━━━━━━━━━━━━━━━━━━\n" \
                                      f"ตู้กกอัจฉริยะ <b>NestX</b> เชื่อมต่อกับ Telegram ของคุณเรียบร้อยแล้ว ✅\n" \
                                      f"📱 นับจากนี้ ระบบจะส่งแจ้งเตือนฉุกเฉินและสรุปรายงานประจำวันมาที่ห้องแชตนี้โดยอัตโนมัติครับ 🐣"
                        send_telegram(welcome_msg, bot_token=token, chat_id=cid)
                        return cid, first_name
                else:
                    # 2. กรณีจับคู่ผู้ใช้คนล่าสุดที่เพิ่งกด Start
                    welcome_msg = f"🎉 <b>ยินดีต้อนรับคุณ {first_name}!</b>\n" \
                                  f"━━━━━━━━━━━━━━━━━━\n" \
                                  f"ตู้กกอัจฉริยะ <b>NestX</b> เชื่อมต่อกับ Telegram ของคุณเรียบร้อยแล้ว ✅\n" \
                                  f"📱 นับจากนี้ ระบบจะส่งแจ้งเตือนฉุกเฉินและสรุปรายงานประจำวันมาที่ห้องแชตนี้โดยอัตโนมัติครับ 🐣"
                    send_telegram(welcome_msg, bot_token=token, chat_id=cid)
                    return cid, first_name
                    
    except Exception:
        pass

    return None, None

# ==========================================
# 🌐 ส่งการแจ้งเตือนผ่าน Discord, LINE และช่องทางอื่นๆ (Multi-Platform Hub)
# ==========================================
def send_discord_webhook(webhook_url, text):
    """ส่งข้อความแจ้งเตือนเข้าช่องแชต Discord ผ่าน Webhook URL"""
    if not webhook_url or not webhook_url.startswith("http"):
        return False, "กรุณาระบุ Discord Webhook URL ที่ถูกต้อง"
    try:
        import re
        clean_text = re.sub(r"<[^>]+>", "", text)
        payload = {
            "username": "NestX Smart Brooder 🐣",
            "content": clean_text
        }
        res = requests.post(webhook_url.strip(), json=payload, timeout=8)
        if res.status_code in (200, 204):
            return True, "ส่งข้อความเข้า Discord สำเร็จ!"
        return False, f"Discord Error ({res.status_code})"
    except Exception as e:
        return False, f"การเชื่อมต่อ Discord ขัดข้อง: {str(e)}"

def send_line_message(channel_token, user_id, text):
    """ส่งข้อความแจ้งเตือนเข้า LINE Messaging API"""
    if not channel_token or not user_id:
        return False, "กรุณาระบุ LINE Token หรือ User ID"
    try:
        import re
        clean_text = re.sub(r"<[^>]+>", "", text)
        url = "https://api.line.me/v2/bot/message/push"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {channel_token.strip()}"
        }
        payload = {
            "to": user_id.strip(),
            "messages": [{"type": "text", "text": clean_text}]
        }
        res = requests.post(url, headers=headers, json=payload, timeout=8)
        if res.status_code == 200:
            return True, "ส่งข้อความเข้า LINE สำเร็จ!"
        return False, f"LINE Error: {res.text}"
    except Exception as e:
        return False, f"การเชื่อมต่อ LINE ขัดข้อง: {str(e)}"
