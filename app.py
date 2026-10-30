import streamlit as st
import pandas as pd
import numpy as np
import time
from datetime import datetime
import os
import json
import html
import plotly.graph_objects as go
import streamlit.components.v1 as components
import importlib

# ==========================================
# 0. ตั้งค่าหน้าจอ (ต้องเป็นคำสั่งแรกสุดของ Streamlit เสมอเพื่อป้องกัน Error บน Cloud)
# ==========================================
st.set_page_config(
    page_title="NestX Smart Brooder",
    page_icon="🐣",
    layout="wide",
    initial_sidebar_state="expanded"
)

try:
    import serial
except BaseException:
    serial = None

try:
    import firebase_admin
    from firebase_admin import credentials, db
except BaseException:
    firebase_admin = None

import db_manager
import telegram_notifier
try:
    importlib.reload(db_manager)
    importlib.reload(telegram_notifier)
except BaseException:
    pass

# ==========================================
# 1. เชื่อมต่อ Firebase Realtime Database (รองรับทั้งไฟล์ในเครื่องและ Streamlit Secrets)
# ==========================================
if firebase_admin is not None and not firebase_admin._apps:
    try:
        if os.path.exists('firebase_key.json'):
            cred = credentials.Certificate('firebase_key.json')
            firebase_admin.initialize_app(cred, {
                'databaseURL': 'https://angry-bird-56bc6-default-rtdb.asia-southeast1.firebasedatabase.app/'
            })
        else:
            try:
                if "firebase_key" in st.secrets:
                    key_dict = dict(st.secrets["firebase_key"])
                    if "private_key" in key_dict and isinstance(key_dict["private_key"], str):
                        key_dict["private_key"] = key_dict["private_key"].replace("\\n", "\n")
                    cred = credentials.Certificate(key_dict)
                    firebase_admin.initialize_app(cred, {
                        'databaseURL': 'https://angry-bird-56bc6-default-rtdb.asia-southeast1.firebasedatabase.app/'
                    })
            except BaseException:
                pass
    except BaseException:
        pass

# ==========================================
# 2. จัดการข้อมูลการตั้งค่าแบบแยกเซสชันรายเครื่อง (Per-Browser Session Isolation)
# ป้องกันไม่ให้บัญชีของคนอื่นที่ล็อกอินจากเครื่องอื่นมาปนกับเครื่องของเรา 100%
# ==========================================
import hashlib
import hmac

PREF_FILE = "user_preferences.json"
_SESSION_SECRET = "nestx_brooder_hmac_secret_2026_key"

# ลบไฟล์เซสชันรวมตัวเก่าที่อาจค้างอยู่บนเซิร์ฟเวอร์ เพื่อป้องกันไม่ให้บัญชีคนอื่นค้างข้ามเครื่อง
if os.path.exists(PREF_FILE):
    try:
        os.remove(PREF_FILE)
    except BaseException:
        pass

def _make_auth_token(username: str) -> str:
    uname = str(username).strip().lower()
    sig = hmac.new(_SESSION_SECRET.encode("utf-8"), uname.encode("utf-8"), hashlib.sha256).hexdigest()[:18]
    return f"{uname}.{sig}"

def _verify_auth_token(token: str) -> str:
    if not token or "." not in str(token):
        return ""
    uname, sig = str(token).rsplit(".", 1)
    expected = hmac.new(_SESSION_SECRET.encode("utf-8"), uname.encode("utf-8"), hashlib.sha256).hexdigest()[:18]
    if hmac.compare_digest(sig, expected):
        return uname
    return ""

# ซิงค์ข้อมูลบัญชีกับ Firebase Cloud ออนไลน์เมื่อเปิดแอปครั้งแรกของเซสชันนี้
if "cloud_accounts_synced" not in st.session_state:
    try:
        if hasattr(db_manager, "sync_all_accounts_with_cloud"):
            db_manager.sync_all_accounts_with_cloud()
    except BaseException:
        pass
    st.session_state.cloud_accounts_synced = True

def _get_default_pref():
    return {
        "farm_name": "NestX",
        "theme": "dark",
        "elec_rate": 4.5,
        "temp_min": 35.0,
        "temp_max": 36.8,
        "hum_min": 50,
        "hum_max": 65,
        "night_mode": False,
        "mode": "🤖 AI ควบคุมอัตโนมัติ (Pi 5)",
        "telegram_token": "",
        "telegram_chat_id": "",
        "telegram_username": "",
        "tg_alert_enabled": True,
        "tg_daily_enabled": True,
        "line_connected": False,
        "line_name": "",
        "line_token": "",
        "line_user_id": "",
        "line_alert_enabled": True,
        "line_daily_enabled": True,
        "discord_connected": False,
        "discord_name": "",
        "discord_webhook": "",
        "discord_alert_enabled": True,
        "discord_daily_enabled": True,
        "email_connected": False,
        "email_address": "",
        "email_daily_enabled": True,
        "account_logged_in": False,
        "account_username": "",
        "account_email": ""
    }

def load_pref():
    # 1. ใช้ข้อมูลเฉพาะของเบราว์เซอร์เครื่องนี้เท่านั้น (st.session_state) ไม่ดึงไฟล์รวมของเซิร์ฟเวอร์มาปนกัน
    if "session_user_pref" in st.session_state:
        return st.session_state["session_user_pref"]

    pref = _get_default_pref()

    # 2. ตรวจสอบว่าเบราว์เซอร์เครื่องนี้มี Auth Token ของตัวเองหรือไม่ (กรณีผู้ใช้กด Refresh หน้าเว็บเดิมของตัวเอง)
    try:
        auth_tok = st.query_params.get("auth", "")
        verified_uname = _verify_auth_token(auth_tok)
        if verified_uname:
            acc_prefs = db_manager.get_user_account_prefs(verified_uname)
            if acc_prefs:
                pref.update(acc_prefs)
                pref["account_logged_in"] = True
                pref["account_username"] = verified_uname
    except BaseException:
        pass

    st.session_state["session_user_pref"] = pref
    return pref

def save_pref(data):
    # บันทึกไว้ในเซสชันเฉพาะเบราว์เซอร์ของเครื่องนี้
    st.session_state["session_user_pref"] = data

    # หากล็อกอินอยู่ ให้บันทึกข้อมูลลงฐานข้อมูลของบัญชีตัวเอง (SQLite + Firebase Cloud) และผูก Token เฉพาะเบราว์เซอร์นี้
    if data.get("account_logged_in") and data.get("account_username"):
        uname = data["account_username"]
        try:
            st.query_params["auth"] = _make_auth_token(uname)
        except BaseException:
            pass
        try:
            db_manager.save_user_account_prefs(uname, data)
        except BaseException:
            pass
    else:
        try:
            if "auth" in st.query_params:
                del st.query_params["auth"]
        except BaseException:
            pass

def logout_current_user(data):
    """ออกจากระบบเฉพาะเครื่องนี้โดยไม่กระทบคนอื่น"""
    if data.get("account_username"):
        try:
            db_manager.save_user_account_prefs(data["account_username"], data)
        except BaseException:
            pass
    st.session_state["session_user_pref"] = _get_default_pref()
    try:
        st.query_params.clear()
    except BaseException:
        pass

user_pref = load_pref()

# สูตรการกกสำเร็จรูป
PRESETS = {
    "🐣 ลูกนกแรกเกิด (1-3 วัน)": {"temp_min": 36.5, "temp_max": 37.2, "hum_min": 55, "hum_max": 65},
    "🪶 เริ่มแตกขนหนาม (4-7 วัน)": {"temp_min": 34.0, "temp_max": 35.5, "hum_min": 50, "hum_max": 60},
    "🦜 ขนคลุมตัว (8+ วัน)": {"temp_min": 31.0, "temp_max": 33.0, "hum_min": 45, "hum_max": 55}
}

PORT = 'COM3' if os.name == 'nt' else '/dev/ttyUSB0'
BAUD_RATE = 115200

if "tare_offset" not in st.session_state:
    st.session_state.tare_offset = 0.0

@st.cache_resource
def init_connection():
    if serial is None:
        return None
    try:
        ser = serial.Serial(PORT, BAUD_RATE, timeout=0.5)
        return ser
    except BaseException:
        return None

ser = init_connection()

def get_sensor_data():
    if ser is not None and ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            data = json.loads(line)
            raw_w = max(0.0, data.get("weight", 0.0) - st.session_state.tare_offset)
            return float(data.get("temp", 35.6)), float(data.get("hum", 58.0)), int(data.get("nh3", 6)), round(raw_w, 1)
        except Exception:
            pass
    return 35.6, 58.0, 6, round(max(0.0, 78.5 - st.session_state.tare_offset), 1)

cur_t, cur_h, cur_nh3, total_weight = get_sensor_data()

# บันทึกค่าลง SQLite DB เป็นประวัติ
try:
    db_manager.log_sensor_reading(
        temp=cur_t, hum=cur_h, nh3=cur_nh3, weight=total_weight,
        heater_power=35, fan_speed=20, bird_count=2, status='Normal'
    )
except Exception:
    pass

# ==========================================
# 4. เมนูด้านข้าง (Sidebar) - แสดงเฉพาะเมื่อเข้าสู่ระบบแล้ว
# ==========================================
is_dark = user_pref.get("theme", "dark") == "dark"
op_mode = user_pref.get("mode", "🤖 AI ควบคุมอัตโนมัติ (Pi 5)")

if user_pref.get("account_logged_in", False):
    with st.sidebar:
        disp_sidebar_name = html.escape(str(user_pref.get("account_display_name") or user_pref.get("account_username", "NestX Owner")))
        st.markdown(f"""
        <div style='text-align: center; padding: 10px 0 12px 0;'>
            <div style='font-size: 2.8rem;'>🐣</div>
            <div style='font-size: 1.45rem; font-weight: 700; color: #f97316; margin-top: 4px; letter-spacing: 0.5px;'>NestX</div>
            <div style='font-size: 0.85rem; color: #94a3b8;'>ตู้กกลูกป้อนอัจฉริยะ 24 ชม.</div>
            <div style='margin-top: 8px; background: rgba(56,189,248,0.12); color: #38bdf8; padding: 4px 12px; border-radius: 12px; font-size: 0.8rem; font-weight: 600; display: inline-block;'>
                👤 {disp_sidebar_name}
            </div>
        </div>
        """, unsafe_allow_html=True)

        if ser:
            st.success(f"🟢 **เชื่อมต่อฮาร์ดแวร์:** {PORT}")
        else:
            st.info("🟡 **โหมดจำลอง (Simulated Live)**")

        st.markdown("---")
        
        # 🎛️ โหมดการทำงาน
        st.markdown("##### 🎛️ โหมดควบคุม")
        op_mode = st.radio(
            "เลือกโหมดควบคุม:",
            ["🤖 AI ควบคุมอัตโนมัติ (Pi 5)", "🖐️ ปรับเอง (Manual)"],
            index=0 if "อัตโนมัติ" in user_pref.get("mode", "อัตโนมัติ") else 1,
            label_visibility="collapsed"
        )
        if op_mode != user_pref.get("mode"):
            user_pref["mode"] = op_mode
            save_pref(user_pref)

        if op_mode == "🤖 AI ควบคุมอัตโนมัติ (Pi 5)":
            st.caption("🔒 **Pi 5 ควบคุมอัตโนมัติ:** คุมอุณหภูมิและความชื้นด้วย PID คงที่ตลอดเวลา")
        else:
            st.markdown("<div style='background:rgba(245,158,11,0.1); border-radius:8px; padding:10px;'>", unsafe_allow_html=True)
            m_heat = st.slider("💡 ไฟกกความร้อน (%)", 0, 100, 45)
            m_fan = st.toggle("🌀 พัดลมดูดระบาย", value=False)
            if st.button("🚀 สั่งการฮาร์ดแวร์", type="primary", use_container_width=True):
                if ser:
                    ser.write((json.dumps({"fan": 1 if m_fan else 0, "light": m_heat}) + '\n').encode('utf-8'))
                st.toast("ส่งคำสั่งเรียบร้อยแล้ว!")
            st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("---")
        
        # 🌙 โหมดกลางคืน
        night_mode = st.toggle("🌙 โหมดกลางคืน (Night Mode)", value=user_pref.get("night_mode", False))
        if night_mode != user_pref.get("night_mode", False):
            user_pref["night_mode"] = night_mode
            save_pref(user_pref)
            st.rerun()

        # ⚖️ เซ็ตศูนย์ตาชั่ง
        if st.button("⚖️ เซ็ตศูนย์ตาชั่ง (Tare 0g)", use_container_width=True):
            st.session_state.tare_offset += total_weight
            st.toast("เซ็ตศูนย์ตาชั่งเรียบร้อยแล้ว")
            st.rerun()

        st.markdown("---")
        # 🎨 ธีมหน้าจอ
        theme_label = "☀️ สลับเป็นโหมดสว่าง" if is_dark else "🌙 สลับเป็นโหมดมืด"
        if st.button(theme_label, use_container_width=True):
            user_pref["theme"] = "light" if is_dark else "dark"
            save_pref(user_pref)
            st.rerun()

        # 🚪 ปุ่มออกจากระบบใน Sidebar (ออกเฉพาะเบราว์เซอร์เครื่องนี้)
        if st.button("🚪 ออกจากระบบ (Log Out)", use_container_width=True):
            logout_current_user(user_pref)
            st.rerun()

# ==========================================
# 5. สไตล์ CSS: เรียบหรู สะอาด สบายตา คมชัดสูงสุด
# ==========================================
if is_dark:
    bg_body = "#090d16"
    card_bg = "#111827"
    card_border = "#1f2937"
    text_color = "#f9fafb"
    sub_color = "#9ca3af"
    metric_bg = "#131c2e"
    btn_bg = "#1e293b"
    btn_text = "#f8fafc"
    btn_border = "#334155"
    input_bg = "#1e293b"
    input_border = "#334155"
else:
    # 🌿 Warm Minimal Light Mode (โทนอุ่น สบายตา นุ่มนวล ไม่ขาวแสบตา)
    bg_body = "#f1f3f5"           # พื้นหลังสีเทาอุ่นนวลตา (ไม่ใช่สีขาวโพลน)
    card_bg = "#ffffff"           # พื้นการ์ดสีขาวสะอาดลอยขึ้นมาอย่างมีมิติ
    card_border = "#e2e6ea"       # ขอบการ์ดบางเบา มินิมอล
    text_color = "#1e293b"        # ตัวหนังสือสีเทาเข้ม อ่านชัดเจน ไม่ดำกระด้าง
    sub_color = "#64748b"         # คำบรรยายสี Slate กำลังดี
    metric_bg = "#f8fafc"         # พื้นหลังกล่องย่อยนวลตา
    btn_bg = "#ffffff"
    btn_text = "#1e293b"
    btn_border = "#cbd5e1"
    input_bg = "#ffffff"
    input_border = "#cbd5e1"

st.markdown(f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Prompt:wght@400;500;600;700&display=swap');

    /* ซ่อนปุ่ม Deploy, เมนู ⋮ มุมขวาบน และ Footer ของ Streamlit ให้ดูเหมือนแอปมือถือแท้ๆ */
    [data-testid="stDeployButton"],
    [data-testid="stToolbarActions"],
    [data-testid="stMainMenu"],
    [data-testid="stStatusWidget"],
    footer {{
        display: none !important;
    }}

    /* กำหนดฟอนต์ Prompt เฉพาะข้อความทั่วไป ป้องกันไม่ให้ทับ Material Icons */
    html, body, p, h1, h2, h3, h4, h5, h6, label, .stMarkdown, .stText, select {{
        font-family: 'Prompt', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif !important;
    }}

    /* ป้องกัน Material Icons ของ Streamlit ไม่ให้แสดงเป็นข้อความดิบ (เช่น _arrow_right_ หรือ visibility) */
    [data-testid="stIconMaterial"], 
    .material-symbols-rounded, 
    .material-icons,
    [data-testid*="Icon"] span,
    button[aria-label*="password"] span,
    button[aria-label*="password"],
    [data-testid="stExpanderToggleIcon"] span,
    [data-testid="stExpanderToggleIcon"],
    i {{
        font-family: "Material Symbols Rounded", "Material Icons", sans-serif !important;
        font-style: normal !important;
        letter-spacing: normal !important;
        text-transform: none !important;
        display: inline-block !important;
        white-space: nowrap !important;
        word-wrap: normal !important;
        direction: ltr !important;
    }}

    [data-testid="stAppViewContainer"] {{
        background-color: {bg_body} !important;
        color: {text_color} !important;
    }}

    [data-testid="stSidebar"] {{
        background-color: {'#0b1120' if is_dark else '#fafbfd'} !important;
        border-right: 1px solid {card_border};
    }}

    /* สไตล์การ์ดคอนเทนเนอร์ Streamlit (st.container border=True) รองรับทุกเวอร์ชัน */
    [data-testid="stVerticalBlockBorderWrapper"],
    [data-testid="stVerticalBlock"] > div:has(> [data-testid="stVerticalBlock"]),
    div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"]),
    .st-emotion-cache-1r6slb0,
    .st-emotion-cache-1wivap2,
    div[data-testid="stVerticalBlock"] > [data-testid="stVerticalBlock"],
    [data-testid="stLayoutWrapper"]:has(> div) {{
        /* fallback styling */
    }}

    /* กำหนดสไตล์การ์ด st.container(border=True) และคีย์เฉพาะจุด */
    div:has(> [data-testid="stVerticalBlockBorderWrapper"]),
    [data-testid="stVerticalBlockBorderWrapper"],
    div[class*="st-key-"],
    .st-key-env_settings_card,
    .st-key-telegram_settings_card,
    .st-key-photo_send_card {{
        background-color: {card_bg} !important;
        border: 1px solid {card_border} !important;
        border-radius: 16px !important;
        box-shadow: {'0 4px 20px rgba(0,0,0,0.3)' if is_dark else '0 3px 14px rgba(15,23,42,0.06)'} !important;
        padding: 20px 22px !important;
    }}

    /* ตัวหนังสือ Label ของ Slider และ Widgets ทุกชนิดในหน้าเว็บ */
    [data-testid="stSlider"] label,
    [data-testid="stSlider"] label p,
    [data-testid="stSlider"] label span,
    [data-testid="stSlider"] [data-testid="stWidgetLabel"] p,
    [data-testid="stSlider"] [data-testid="stWidgetLabel"] span,
    [data-testid="stSlider"] div[data-testid="stMarkdownContainer"] p,
    [data-testid="stTextInput"] label,
    [data-testid="stTextInput"] label p,
    [data-testid="stTextInput"] label span,
    [data-testid="stTextArea"] label,
    [data-testid="stTextArea"] label p,
    [data-testid="stTextArea"] label span,
    [data-testid="stRadio"] label,
    [data-testid="stRadio"] label p,
    [data-testid="stRadio"] label span,
    [data-testid="stWidgetLabel"] label,
    [data-testid="stWidgetLabel"] p,
    [data-testid="stWidgetLabel"] span,
    label p,
    label span {{
        color: {text_color} !important;
        font-weight: 600 !important;
        font-size: 0.95rem !important;
    }}

    /* ตัวเลขค่าและสเกลบน Slider (Tick & Value) */
    [data-testid="stSlider"] div[data-testid="stSliderThumbValue"],
    [data-testid="stSlider"] div[data-testid="stTickBarMin"],
    [data-testid="stSlider"] div[data-testid="stTickBarMax"],
    [data-testid="stSlider"] div[role="slider"],
    div[data-testid="stSlider"] [data-baseweb="slider"] div {{
        color: {text_color} !important;
        font-weight: 600 !important;
    }}

    /* สไตล์ปุ่มกดทุกปุ่มในระบบอย่างเด็ดขาด (ป้องกันปุ่มขาวในโหมดมืด) */
    button,
    button[data-testid*="BaseButton"],
    button[data-testid*="baseButton"],
    [data-testid="stButton"] button,
    div[data-testid="stButton"] > button,
    .stButton > button,
    button[kind="secondary"] {{
        background-color: {btn_bg} !important;
        background: {btn_bg} !important;
        color: {btn_text} !important;
        border: 1.5px solid {btn_border} !important;
        border-radius: 12px !important;
        font-weight: 600 !important;
        font-family: 'Prompt', sans-serif !important;
        white-space: normal !important;
        height: auto !important;
        min-height: 44px !important;
        line-height: 1.4 !important;
        padding: 10px 14px !important;
        transition: all 0.2s ease-in-out !important;
        box-shadow: {'0 2px 8px rgba(0,0,0,0.25)' if is_dark else '0 1px 4px rgba(0,0,0,0.06)'} !important;
    }}
    button p,
    button span,
    button div,
    [data-testid="stButton"] button p,
    [data-testid="stButton"] button span,
    .stButton > button p,
    .stButton > button span {{
        color: {btn_text} !important;
        font-family: 'Prompt', sans-serif !important;
        font-weight: 600 !important;
        white-space: normal !important;
        overflow: visible !important;
        text-overflow: unset !important;
        word-break: break-word !important;
        line-height: 1.38 !important;
    }}
    button:hover,
    button[data-testid*="BaseButton"]:hover,
    [data-testid="stButton"] button:hover,
    .stButton > button:hover {{
        background-color: #f97316 !important;
        background: #f97316 !important;
        color: #ffffff !important;
        border-color: #f97316 !important;
        box-shadow: 0 4px 16px rgba(249, 115, 22, 0.45) !important;
        transform: translateY(-1px);
    }}
    button:hover p,
    button:hover span,
    button:hover div,
    [data-testid="stButton"] button:hover p {{
        color: #ffffff !important;
    }}
    button:active,
    .stButton > button:active {{
        transform: translateY(1px);
    }}

    /* สไตล์ปุ่มประเภท Primary */
    button[kind="primary"],
    .stButton > button[kind="primary"] {{
        background: linear-gradient(135deg, #f97316 0%, #ea580c 100%) !important;
        color: #ffffff !important;
        border: none !important;
    }}
    button[kind="primary"] p,
    button[kind="primary"] span {{
        color: #ffffff !important;
    }}
    button[kind="primary"]:hover {{
        background: #ea580c !important;
        color: #ffffff !important;
    }}

    /* ตัวหนังสือตัวเลือก Radio/Checkbox และ Label ใน Sidebar */
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
    [data-testid="stRadio"] label p,
    [data-testid="stRadio"] label span {{
        color: {text_color} !important;
        font-weight: 500 !important;
    }}
    [data-testid="stSidebar"] h5,
    [data-testid="stSidebar"] .stMarkdown h5 {{
        color: {text_color} !important;
        font-weight: 700 !important;
    }}

    /* กล่องข้อความและ Input ต่างๆ */
    input[type="text"],
    input[type="password"],
    input[type="number"],
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input {{
        background-color: {input_bg} !important;
        color: {text_color} !important;
        -webkit-text-fill-color: {text_color} !important;
        border: 1.5px solid {input_border} !important;
        border-radius: 10px !important;
        font-family: 'Prompt', sans-serif !important;
        font-size: 0.95rem !important;
        padding: 10px 14px !important;
    }}
    input:focus,
    [data-testid="stTextInput"] input:focus {{
        border-color: #38bdf8 !important;
        box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.25) !important;
    }}

    /* กล่องพิมพ์ข้อความหลายบรรทัด (Text Area) */
    textarea,
    [data-testid="stTextArea"] textarea {{
        background-color: {input_bg} !important;
        color: {text_color} !important;
        -webkit-text-fill-color: {text_color} !important;
        border: 1.5px solid {input_border} !important;
        border-radius: 10px !important;
        font-family: 'Prompt', sans-serif !important;
    }}
    textarea::placeholder {{
        color: {'#94a3b8' if is_dark else '#64748b'} !important;
        -webkit-text-fill-color: {'#94a3b8' if is_dark else '#64748b'} !important;
    }}

    /* กล่องอัปโหลดรูป (File Uploader) */
    [data-testid="stFileUploaderDropzone"],
    [data-testid="stFileUploader"] section {{
        background-color: {input_bg} !important;
        border: 1.5px dashed {input_border} !important;
        border-radius: 12px !important;
    }}
    [data-testid="stFileUploaderDropzone"] span,
    [data-testid="stFileUploaderDropzone"] small,
    [data-testid="stFileUploaderDropzoneInstructions"] * {{
        color: {sub_color} !important;
    }}
    [data-testid="stFileUploader"] [data-testid="stFileUploaderFileName"],
    [data-testid="stFileUploader"] small {{
        color: {text_color} !important;
    }}

    /* ข้อความ Placeholder ในช่องกรอก สว่างอ่านง่าย */
    input::placeholder,
    [data-testid="stTextInput"] input::placeholder {{
        color: {'#94a3b8' if is_dark else '#64748b'} !important;
        -webkit-text-fill-color: {'#94a3b8' if is_dark else '#64748b'} !important;
        opacity: 0.9 !important;
    }}

    /* ข้อความคำอธิบาย (Caption) และข้อความย่อยทั้งหมด ชัดเจน อ่านง่าย ไม่จาง */
    .stCaption, 
    [data-testid="stCaptionContainer"],
    [data-testid="stCaptionContainer"] p,
    [data-testid="stCaptionContainer"] span,
    small,
    .stMarkdown small {{
        color: {'#cbd5e1' if is_dark else '#334155'} !important;
        font-size: 0.88rem !important;
        font-weight: 500 !important;
    }}

    /* สไตล์ Tabs ให้สะอาดตา คมชัด อ่านง่ายทั้งโหมดมืดและสว่าง */
    .stTabs [data-baseweb="tab-list"] {{
        gap: 12px;
        background: transparent;
        padding-bottom: 12px;
        border-bottom: 2px solid {card_border};
    }}
    .stTabs [data-baseweb="tab"] {{
        background: transparent !important;
        border: none !important;
        border-radius: 8px;
        color: {'#94a3b8' if is_dark else '#64748b'} !important;
        padding: 8px 18px;
        font-size: 1.05rem;
        font-weight: 600;
    }}
    .stTabs [data-baseweb="tab"] p,
    .stTabs [data-baseweb="tab"] span {{
        color: {'#94a3b8' if is_dark else '#64748b'} !important;
        font-weight: 600 !important;
    }}
    .stTabs [data-baseweb="tab"]:hover {{
        color: {'#f1f5f9' if is_dark else '#1e293b'} !important;
    }}
    .stTabs [data-baseweb="tab"]:hover p,
    .stTabs [data-baseweb="tab"]:hover span {{
        color: {'#f1f5f9' if is_dark else '#1e293b'} !important;
    }}
    .stTabs [aria-selected="true"] {{
        background: transparent !important;
        color: #f97316 !important;
        border-bottom: 3px solid #f97316 !important;
        border-radius: 0 !important;
        box-shadow: none !important;
    }}
    .stTabs [aria-selected="true"] p,
    .stTabs [aria-selected="true"] span {{
        color: #f97316 !important;
        font-weight: 700 !important;
    }}

    /* หัวข้อบนสุด (Hero Bar) */
    .top-hero {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        background: {card_bg};
        border: 1px solid {card_border};
        border-radius: 16px;
        padding: 18px 24px;
        margin-bottom: 20px;
    }}
    .farm-title {{
        font-size: 1.5rem;
        font-weight: 700;
        color: {text_color};
    }}
    .farm-subtitle {{
        font-size: 0.9rem;
        color: {sub_color};
        margin-top: 3px;
    }}

    /* การ์ดเซนเซอร์ขนาดใหญ่ อ่านง่าย ชัดเจน */
    .sensor-card {{
        background: {card_bg};
        border: 1px solid {card_border};
        border-radius: 16px;
        padding: 22px 20px;
        text-align: center;
        box-shadow: 0 3px 12px rgba(15, 23, 42, {'0.25' if is_dark else '0.04'});
        position: relative;
        overflow: hidden;
    }}
    .sensor-icon {{
        font-size: 2rem;
        margin-bottom: 6px;
    }}
    .sensor-name {{
        font-size: 1rem;
        font-weight: 600;
        color: {sub_color};
        margin-bottom: 4px;
    }}
    .sensor-number {{
        font-size: 2.8rem;
        font-weight: 700;
        line-height: 1.1;
        margin-bottom: 8px;
    }}
    .sensor-unit {{
        font-size: 1.2rem;
        font-weight: 500;
        color: {sub_color};
        margin-left: 2px;
    }}
    .sensor-badge {{
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 0.82rem;
        font-weight: 600;
    }}

    /* กล่องข้อมูลย่อย */
    .info-card {{
        background: {card_bg};
        border: 1px solid {card_border};
        border-radius: 16px;
        padding: 20px;
        height: 100%;
        box-shadow: 0 3px 12px rgba(15, 23, 42, {'0.25' if is_dark else '0.04'});
    }}
    .device-row {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 6px;
        font-weight: 600;
    }}

    /* 📱 Responsive Mobile UI (สำหรับหน้าจอมือถือและการเปิดผ่าน LINE / Instagram / Safari / Chrome) */
    @media (max-width: 768px) {{
        .block-container,
        [data-testid="stAppViewBlockContainer"] {{
            padding: 2.2rem 0.75rem 2.5rem 0.75rem !important;
            max-width: 100% !important;
        }}
        .top-hero {{
            flex-direction: column !important;
            align-items: flex-start !important;
            gap: 10px !important;
            padding: 14px 16px !important;
            margin-bottom: 14px !important;
        }}
        .farm-title {{
            font-size: 1.22rem !important;
            line-height: 1.35 !important;
        }}
        .farm-subtitle {{
            font-size: 0.8rem !important;
            line-height: 1.5 !important;
        }}
        .sensor-card {{
            padding: 14px 12px !important;
            margin-bottom: 10px !important;
        }}
        .sensor-icon {{
            font-size: 1.6rem !important;
            margin-bottom: 4px !important;
        }}
        .sensor-number {{
            font-size: 2.15rem !important;
            margin-bottom: 6px !important;
        }}
        .sensor-unit {{
            font-size: 1rem !important;
        }}
        .info-card {{
            padding: 14px !important;
            margin-bottom: 12px !important;
        }}
        div:has(> [data-testid="stVerticalBlockBorderWrapper"]),
        [data-testid="stVerticalBlockBorderWrapper"],
        div[class*="st-key-"] {{
            padding: 14px 14px !important;
            border-radius: 14px !important;
        }}
        .stTabs [data-baseweb="tab-list"] {{
            overflow-x: auto !important;
            flex-wrap: nowrap !important;
            -webkit-overflow-scrolling: touch !important;
            gap: 4px !important;
            padding-bottom: 8px !important;
            scrollbar-width: none !important;
        }}
        .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar {{
            display: none !important;
        }}
        .stTabs [data-baseweb="tab"] {{
            padding: 6px 12px !important;
            font-size: 0.88rem !important;
            white-space: nowrap !important;
            flex-shrink: 0 !important;
        }}
        button,
        [data-testid="stButton"] button,
        .stButton > button {{
            padding: 10px 12px !important;
            font-size: 0.88rem !important;
        }}
        button p,
        button span,
        [data-testid="stButton"] button p {{
            font-size: 0.88rem !important;
        }}
        h3, .stMarkdown h3 {{
            font-size: 1.18rem !important;
        }}
        h5, .stMarkdown h5 {{
            font-size: 0.98rem !important;
            line-height: 1.4 !important;
        }}
    }}
</style>
""", unsafe_allow_html=True)

# ==========================================
# 🔐 หน้าบังคับเข้าสู่ระบบ / สมัครสมาชิก (Full-Page Auth Gate หน้าแรกสุด)
# หากล็อกอินแล้วจะข้ามหน้านี้ไปเข้าแดชบอร์ดอัตโนมัติทุกครั้งจนกว่าจะกดออกจากระบบ
# ==========================================
if not user_pref.get("account_logged_in", False):
    st.markdown("""
    <style>
        [data-testid="stSidebar"], [data-testid="collapsedControl"] {
            display: none !important;
        }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("<div style='height:28px;'></div>", unsafe_allow_html=True)
    gate_l, gate_c, gate_r = st.columns([1, 1.35, 1])

    with gate_c:
        with st.container(border=True, key="fullpage_auth_card"):
            st.markdown(f"""
            <div style='text-align:center; padding:12px 0 8px 0;'>
                <div style='width:72px; height:72px; margin:0 auto 10px auto; border-radius:50%; background:linear-gradient(135deg, #f97316, #ea580c); display:flex; align-items:center; justify-content:center; font-size:2.4rem; box-shadow:0 6px 20px rgba(249,115,22,0.35);'>
                    🐣
                </div>
                <div style='font-size:1.65rem; font-weight:700; color:{text_color}; letter-spacing:0.5px;'>NestX Smart Brooder</div>
                <div style='font-size:0.9rem; color:{sub_color}; margin-top:4px; margin-bottom:10px;'>
                    ระบบตู้กกลูกป้อนอัจฉริยะ 24 ชั่วโมง • เข้าสู่ระบบหรือสมัครบัญชีเพื่อเริ่มใช้งาน
                </div>
                <div style='display:inline-block; background:rgba(34,197,94,0.14); color:#22c55e; border:1px solid rgba(34,197,94,0.3); padding:4px 12px; border-radius:20px; font-size:0.78rem; font-weight:600; margin-bottom:6px;'>
                    ☁️ ออนไลน์: เชื่อมต่อ Firebase Cloud & ฐานข้อมูลระบบเรียบร้อย
                </div>
                <div style='font-size:0.74rem; color:#38bdf8; font-weight:600; margin-bottom:10px;'>
                    🛡️ ระบบป้องกัน OWASP Security: เข้ารหัส PBKDF2-SHA256 + Salt • ล็อคกันสุ่มรหัส 5 ครั้ง • ป้องกัน SQLi & XSS
                </div>
            </div>
            """, unsafe_allow_html=True)

            gate_options = [
                "🔑 เข้าสู่ระบบ (Sign In)",
                "📝 สมัครบัญชีใหม่ (Sign Up)",
                "🔓 กู้รหัสผ่าน (Telegram OTP)"
            ]
            if st.session_state.get("jump_to_forgot_pw"):
                st.session_state["gate_auth_radio"] = "🔓 กู้รหัสผ่าน (Telegram OTP)"
                st.session_state["jump_to_forgot_pw"] = False

            gate_mode = st.radio(
                "เลือกโหมดการเข้าใช้งาน",
                gate_options,
                horizontal=True,
                label_visibility="collapsed",
                key="gate_auth_radio"
            )
            st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)

            if gate_mode == "🔑 เข้าสู่ระบบ (Sign In)":
                g_user = st.text_input("ชื่อผู้ใช้ หรือ อีเมล (Username / Email):", placeholder="เช่น xuelihkl หรือ paritchayasueakla@gmail.com", key="gate_login_user")
                g_pass = st.text_input("รหัสผ่าน (Password):", type="password", placeholder="กรอกรหัสผ่านของคุณ", key="gate_login_pass")
                st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)

                btn_si_col, btn_fp_col = st.columns([1.6, 1])
                with btn_si_col:
                    if st.button("🚀 เข้าสู่ระบบ (Sign In)", type="primary", use_container_width=True, key="gate_btn_signin"):
                        if not g_user.strip() or not g_pass:
                            st.warning("กรุณากรอกชื่อผู้ใช้/อีเมล และรหัสผ่านให้ครบครับ")
                        else:
                            ok, msg, acc_info = db_manager.authenticate_user(g_user, g_pass)
                            if ok and acc_info:
                                saved_p = acc_info.get("preferences", {})
                                clean_acct_defaults = {
                                    "farm_name": saved_p.get("farm_name", f"ฟาร์มของ {acc_info['display_name']}"),
                                    "telegram_chat_id": saved_p.get("telegram_chat_id", ""),
                                    "telegram_username": saved_p.get("telegram_username", ""),
                                    "tg_alert_enabled": saved_p.get("tg_alert_enabled", True),
                                    "tg_daily_enabled": saved_p.get("tg_daily_enabled", True),
                                    "line_connected": saved_p.get("line_connected", False),
                                    "line_name": saved_p.get("line_name", ""),
                                    "line_token": saved_p.get("line_token", ""),
                                    "line_user_id": saved_p.get("line_user_id", ""),
                                    "discord_connected": saved_p.get("discord_connected", False),
                                    "discord_name": saved_p.get("discord_name", ""),
                                    "discord_webhook": saved_p.get("discord_webhook", ""),
                                    "email_connected": saved_p.get("email_connected", False),
                                    "email_address": saved_p.get("email_address", acc_info.get("email", "")),
                                    "temp_min": saved_p.get("temp_min", 35.0),
                                    "temp_max": saved_p.get("temp_max", 36.8),
                                    "hum_min": saved_p.get("hum_min", 50),
                                    "hum_max": saved_p.get("hum_max", 65),
                                    "account_logged_in": True,
                                    "account_username": acc_info["username"],
                                    "account_display_name": acc_info["display_name"],
                                    "account_email": acc_info.get("email", "")
                                }
                                user_pref.update(clean_acct_defaults)
                                save_pref(user_pref)
                                st.toast("✅ " + msg)
                                time.sleep(0.4)
                                st.rerun()
                            else:
                                st.error(msg)
                with btn_fp_col:
                    if st.button("🔓 ลืมรหัสผ่าน?", use_container_width=True, key="gate_btn_jump_forgot", help="กู้คืนรหัสผ่านด้วยรหัสลับ OTP ทาง Telegram"):
                        if g_user.strip():
                            st.session_state["forgot_target_user"] = g_user.strip()
                        st.session_state["jump_to_forgot_pw"] = True
                        st.rerun()

            elif gate_mode == "📝 สมัครบัญชีใหม่ (Sign Up)":
                g_reg_user = st.text_input("ตั้งชื่อผู้ใช้ (Username สำหรับล็อกอิน)*:", placeholder="เช่น farm_a หรือ somchai", key="gate_reg_user")
                g_reg_disp = st.text_input("ชื่อฟาร์ม / ชื่อที่แสดง (Display Name)*:", placeholder="เช่น ฟาร์มนกแก้วสมชาย", key="gate_reg_disp")
                g_reg_email = st.text_input("อีเมล (Email - ไม่บังคับ):", placeholder="เช่น somchai@gmail.com", key="gate_reg_email")
                gp1, gp2 = st.columns(2)
                with gp1:
                    g_reg_pass = st.text_input("ตั้งรหัสผ่าน*:", type="password", placeholder="อย่างน้อย 4 ตัวอักษร", key="gate_reg_p1")
                with gp2:
                    g_reg_pass_cf = st.text_input("ยืนยันรหัสผ่าน*:", type="password", placeholder="กรอกรหัสผ่านซ้ำ", key="gate_reg_p2")

                st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)
                if st.button("✨ สมัครสมาชิกและเข้าสู่ระบบทันที", type="primary", use_container_width=True, key="gate_btn_signup"):
                    if not g_reg_user.strip() or not g_reg_pass:
                        st.warning("กรุณากรอก Username และรหัสผ่านให้ครบถ้วนครับ")
                    elif g_reg_pass != g_reg_pass_cf:
                        st.error("รหัสผ่านทั้ง 2 ช่องไม่ตรงกัน กรุณาตรวจสอบอีกครั้งครับ")
                    else:
                        disp_final = g_reg_disp.strip() or g_reg_user.strip()
                        init_account_prefs = {
                            "farm_name": disp_final,
                            "theme": user_pref.get("theme", "dark"),
                            "elec_rate": 4.5,
                            "temp_min": 35.0,
                            "temp_max": 36.8,
                            "hum_min": 50,
                            "hum_max": 65,
                            "telegram_chat_id": "",
                            "telegram_username": "",
                            "line_connected": False,
                            "discord_connected": False,
                            "email_connected": bool(g_reg_email.strip()),
                            "email_address": g_reg_email.strip(),
                            "account_username": g_reg_user.strip().lower(),
                            "account_display_name": disp_final,
                            "account_email": g_reg_email.strip()
                        }
                        ok, msg = db_manager.register_user(
                            username=g_reg_user,
                            password=g_reg_pass,
                            display_name=disp_final,
                            email=g_reg_email,
                            initial_prefs=init_account_prefs
                        )
                        if ok:
                            user_pref.update(init_account_prefs)
                            user_pref["account_logged_in"] = True
                            save_pref(user_pref)
                            st.toast("🎉 " + msg)
                            time.sleep(0.5)
                            st.rerun()
                        else:
                            st.error(msg)

            else:
                # 🔓 โหมดกู้คืนรหัสผ่านผ่าน Telegram OTP (Forgot Password via Telegram)
                st.markdown(f"""
                <div style='background:rgba(56,189,248,0.1); border:1px solid rgba(56,189,248,0.3); border-radius:12px; padding:12px 14px; margin-bottom:12px;'>
                    <div style='font-weight:700; color:#38bdf8; font-size:0.96rem;'>✈️ กู้คืนรหัสผ่านผ่านแอป Telegram (OTP 6 หลัก)</div>
                    <div style='font-size:0.82rem; color:{sub_color}; margin-top:3px;'>
                        หากบัญชีของคุณเคยผูก Telegram (@TheNestXBot) ไว้ ระบบจะส่ง <b>รหัสลับ OTP 6 หลัก</b> เข้าแชต Telegram ของคุณเพื่อใช้ตั้งรหัสผ่านใหม่ทันทีครับ
                    </div>
                </div>
                """, unsafe_allow_html=True)

                default_forgot_u = st.session_state.get("forgot_target_user", "")
                fp_user = st.text_input(
                    "1️⃣ กรอกชื่อผู้ใช้ หรือ อีเมล ของบัญชีที่ต้องการกู้รหัสผ่าน:",
                    value=default_forgot_u,
                    placeholder="เช่น xuelihkl หรือ paritchayasueakla@gmail.com",
                    key="gate_fp_user_input"
                )

                if st.button("📨 ส่งรหัสลับ OTP 6 หลักเข้า Telegram ของฉัน", use_container_width=True, key="gate_btn_send_otp"):
                    if not fp_user.strip():
                        st.warning("กรุณากรอกชื่อผู้ใช้หรืออีเมลก่อนกดรับรหัส OTP ครับ")
                    else:
                        ok_otp, msg_otp, otp_info = db_manager.request_password_reset_otp(fp_user.strip())
                        if ok_otp and otp_info:
                            st.session_state["otp_sent_for_user"] = otp_info["username"]
                            st.session_state["otp_sent_msg"] = msg_otp
                            st.toast(msg_otp)
                        else:
                            st.error(msg_otp)

                if st.session_state.get("otp_sent_msg") and st.session_state.get("otp_sent_for_user"):
                    st.success(st.session_state["otp_sent_msg"])

                st.markdown("<hr style='margin:12px 0; border-color:rgba(148,163,184,0.2);'>", unsafe_allow_html=True)
                fp_otp = st.text_input(
                    "2️⃣ กรอกรหัสลับ OTP 6 หลัก (ที่ได้รับใน Telegram):",
                    placeholder="เช่น 482910",
                    max_chars=6,
                    key="gate_fp_otp_code"
                )
                np_col1, np_col2 = st.columns(2)
                with np_col1:
                    fp_new_p1 = st.text_input("ตั้งรหัสผ่านใหม่*:", type="password", placeholder="อย่างน้อย 4 ตัวอักษร", key="gate_fp_np1")
                with np_col2:
                    fp_new_p2 = st.text_input("ยืนยันรหัสผ่านใหม่*:", type="password", placeholder="กรอกรหัสผ่านใหม่ซ้ำ", key="gate_fp_np2")

                if st.button("✅ ยืนยันรหัส OTP และตั้งรหัสผ่านใหม่ทันที", type="primary", use_container_width=True, key="gate_btn_verify_otp"):
                    target_ident = fp_user.strip() or st.session_state.get("otp_sent_for_user", "")
                    if not target_ident or not fp_otp.strip() or not fp_new_p1:
                        st.warning("กรุณากรอกชื่อผู้ใช้, รหัส OTP 6 หลัก และรหัสผ่านใหม่ให้ครบถ้วนครับ")
                    elif fp_new_p1 != fp_new_p2:
                        st.error("รหัสผ่านใหม่ทั้ง 2 ช่องไม่ตรงกัน กรุณาตรวจสอบอีกครั้งครับ")
                    else:
                        ok_rst, msg_rst, rst_acc_info = db_manager.verify_otp_and_reset_password(
                            target_ident, fp_otp.strip(), fp_new_p1
                        )
                        if ok_rst and rst_acc_info:
                            st.session_state.pop("otp_sent_for_user", None)
                            st.session_state.pop("otp_sent_msg", None)
                            saved_p = rst_acc_info.get("preferences", {})
                            clean_acct_defaults = {
                                "farm_name": saved_p.get("farm_name", f"ฟาร์มของ {rst_acc_info['display_name']}"),
                                "telegram_chat_id": saved_p.get("telegram_chat_id", ""),
                                "telegram_username": saved_p.get("telegram_username", ""),
                                "tg_alert_enabled": saved_p.get("tg_alert_enabled", True),
                                "tg_daily_enabled": saved_p.get("tg_daily_enabled", True),
                                "line_connected": saved_p.get("line_connected", False),
                                "line_name": saved_p.get("line_name", ""),
                                "line_token": saved_p.get("line_token", ""),
                                "line_user_id": saved_p.get("line_user_id", ""),
                                "discord_connected": saved_p.get("discord_connected", False),
                                "discord_name": saved_p.get("discord_name", ""),
                                "discord_webhook": saved_p.get("discord_webhook", ""),
                                "email_connected": saved_p.get("email_connected", False),
                                "email_address": saved_p.get("email_address", rst_acc_info.get("email", "")),
                                "temp_min": saved_p.get("temp_min", 35.0),
                                "temp_max": saved_p.get("temp_max", 36.8),
                                "hum_min": saved_p.get("hum_min", 50),
                                "hum_max": saved_p.get("hum_max", 65),
                                "account_logged_in": True,
                                "account_username": rst_acc_info["username"],
                                "account_display_name": rst_acc_info["display_name"],
                                "account_email": rst_acc_info.get("email", "")
                            }
                            user_pref.update(clean_acct_defaults)
                            save_pref(user_pref)
                            st.toast(msg_rst)
                            time.sleep(0.5)
                            st.rerun()
                        else:
                            st.error(msg_rst)

            st.markdown(f"<div style='text-align:center; font-size:0.78rem; color:{sub_color}; margin-top:14px;'>🔒 ระบบจะจดจำการเข้าสู่ระบบของคุณไว้อัตโนมัติ ไม่ต้องล็อกอินซ้ำในการเข้าใช้งานครั้งถัดไป</div>", unsafe_allow_html=True)

    st.stop()

# ==========================================
# 6. แถบหัวเรื่องหลัก (Top Hero Bar)
# ==========================================
t_min = float(user_pref.get("temp_min", 35.0))
t_max = float(user_pref.get("temp_max", 36.8))
h_min = int(user_pref.get("hum_min", 50))
h_max = int(user_pref.get("hum_max", 65))

night_badge = "<span style='background:#4338ca; color:#ffffff; padding:4px 12px; border-radius:20px; font-size:0.8rem; margin-left:8px;'>🌙 กลางคืน</span>" if user_pref.get("night_mode") else ""

st.markdown(f"""
<div class='top-hero'>
    <div>
        <div class='farm-title'>🐣 {html.escape(str(user_pref.get('farm_name', 'Smart Baby Bird Brooder')))} {night_badge}</div>
        <div class='farm-subtitle'>
            🎯 <b>อุณหภูมิ:</b> {t_min:.1f}-{t_max:.1f}°C &nbsp;|&nbsp; 
            💧 <b>ความชื้น:</b> {h_min}-{h_max}%RH &nbsp;|&nbsp; 
            📷 <b>นกในตู้:</b> 2 ตัว
        </div>
    </div>
    <div style='display:flex; gap:10px; align-items:center; flex-wrap:wrap;'>
        <div style='background: {"rgba(34, 197, 94, 0.15)" if "อัตโนมัติ" in op_mode else "rgba(245, 158, 11, 0.15)"}; color: {"#22c55e" if "อัตโนมัติ" in op_mode else "#f59e0b"}; padding: 5px 12px; border-radius: 20px; font-weight: 600; font-size: 0.84rem; border: 1px solid {"#22c55e" if "อัตโนมัติ" in op_mode else "#f59e0b"};'>
            {op_mode}
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

# ==========================================
# 7. แถบเมนูหลัก 5 แท็บ (กระชับ อ่านง่ายทั้งบนคอมและมือถือ)
# ==========================================
tab_dash, tab_history, tab_sim, tab_account, tab_settings = st.tabs([
    "🏠 แดชบอร์ด",
    "📈 กราฟสถิติ",
    "🧪 AI Simulator",
    "👤 บัญชี & แจ้งเตือน",
    "⚙️ ตั้งค่าระบบ"
])

# --------------------------------------------------------------------------
# แท็บ 1: แดชบอร์ดภาพรวม (4 การ์ดใหญ่ เด่นชัด อ่านง่ายใน 0.5 วินาที)
# --------------------------------------------------------------------------
with tab_dash:
    # --- แถวที่ 1: 4 การ์ดเซนเซอร์หลักขนาดใหญ่ ---
    col1, col2, col3, col4 = st.columns(4)

    # เช็คสถานะอุณหภูมิ
    if t_min <= cur_t <= t_max:
        t_status_text = "🟢 อุณหภูมิเหมาะสม"
        t_status_bg = "rgba(34, 197, 94, 0.15)"
        t_status_col = "#22c55e"
    elif cur_t < t_min:
        t_status_text = "🟡 หนาวกว่าเกณฑ์"
        t_status_bg = "rgba(245, 158, 11, 0.15)"
        t_status_col = "#f59e0b"
    else:
        t_status_text = "🔴 ร้อนกว่าเกณฑ์"
        t_status_bg = "rgba(239, 68, 68, 0.15)"
        t_status_col = "#ef4444"

    with col1:
        st.markdown(f"""<div class='sensor-card' style='border-top: 5px solid #f97316;'>
<div class='sensor-icon'>🌡️</div>
<div class='sensor-name'>อุณหภูมิตู้กก</div>
<div class='sensor-number' style='color: #f97316;'>{cur_t:.1f}<span class='sensor-unit'>°C</span></div>
<div class='sensor-badge' style='background: {t_status_bg}; color: {t_status_col};'>{t_status_text}</div>
<div style='font-size: 0.8rem; color: {sub_color}; margin-top: 8px;'>เป้าหมาย: {t_min:.1f} - {t_max:.1f} °C</div>
</div>""", unsafe_allow_html=True)

    with col2:
        st.markdown(f"""<div class='sensor-card' style='border-top: 5px solid #38bdf8;'>
<div class='sensor-icon'>💧</div>
<div class='sensor-name'>ความชื้นสัมพัทธ์</div>
<div class='sensor-number' style='color: #38bdf8;'>{cur_h:.0f}<span class='sensor-unit'>%RH</span></div>
<div class='sensor-badge' style='background: rgba(56, 189, 248, 0.15); color: #38bdf8;'>🟢 ความชื้นพอดี</div>
<div style='font-size: 0.8rem; color: {sub_color}; margin-top: 8px;'>เป้าหมาย: {h_min} - {h_max} %RH</div>
</div>""", unsafe_allow_html=True)

    with col3:
        st.markdown(f"""<div class='sensor-card' style='border-top: 5px solid #22c55e;'>
<div class='sensor-icon'>💨</div>
<div class='sensor-name'>แก๊สแอมโมเนีย</div>
<div class='sensor-number' style='color: #22c55e;'>{cur_nh3}<span class='sensor-unit'>ppm</span></div>
<div class='sensor-badge' style='background: rgba(34, 197, 94, 0.15); color: #22c55e;'>🟢 ปลอดภัย ไร้กลิ่น</div>
<div style='font-size: 0.8rem; color: {sub_color}; margin-top: 8px;'>เกณฑ์ปลอดภัย: ต่ำกว่า 10 ppm</div>
</div>""", unsafe_allow_html=True)

    with col4:
        st.markdown(f"""<div class='sensor-card' style='border-top: 5px solid #a855f7;'>
<div class='sensor-icon'>⚖️</div>
<div class='sensor-name'>น้ำหนักรวมในตู้</div>
<div class='sensor-number' style='color: #a855f7;'>{total_weight:.1f}<span class='sensor-unit'>g</span></div>
<div class='sensor-badge' style='background: rgba(168, 85, 247, 0.15); color: #a855f7;'>📈 ชั่งสดอัตโนมัติ</div>
<div style='font-size: 0.8rem; color: {sub_color}; margin-top: 8px;'>หักน้ำหนักรังแล้ว (Net Tare)</div>
</div>""", unsafe_allow_html=True)

    st.markdown("<div style='height:20px;'></div>", unsafe_allow_html=True)

    # --- แถวที่ 2: กล้องตรวจจับ AI และสถานะอุปกรณ์ควบคุม ---
    row2_left, row2_right = st.columns([1.3, 1])

    with row2_left:
        st.markdown(f"""<div class='info-card'>
<div class='device-row' style='margin-bottom:12px;'>
<span style='font-weight:700; font-size:1.05rem;'>📷 กล้อง AI ตรวจจับลูกนก</span>
<span style='background:#ef4444; color:#ffffff; padding:2px 10px; border-radius:12px; font-size:0.75rem; font-weight:700;'>LIVE</span>
</div>
<div style='background:#000000; border-radius:12px; padding:20px 14px; text-align:center; min-height:200px; display:flex; flex-direction:column; justify-content:center; align-items:center; border:1px solid #334155;'>
<div style='font-size:3rem; margin-bottom:8px;'>🐣 🐣</div>
<div style='color:#22c55e; font-weight:700; font-size:1.02rem;'>YOLOv8 ตรวจพบลูกนกในตู้ 2 ตัว</div>
<div style='color:#94a3b8; font-size:0.82rem; margin-top:4px;'>สถานะ: นอนหลับสบาย อุณหภูมิอบอุ่นพอเหมาะ</div>
<div style='margin-top:12px; display:flex; gap:8px; flex-wrap:wrap; justify-content:center;'>
<span style='background:rgba(34,197,94,0.2); color:#22c55e; padding:3px 10px; border-radius:8px; font-size:0.76rem;'>Bird #1: 38.2g (98%)</span>
<span style='background:rgba(56,189,248,0.2); color:#38bdf8; padding:3px 10px; border-radius:8px; font-size:0.76rem;'>Bird #2: 40.3g (97%)</span>
</div>
</div>
<div style='font-size:0.8rem; color:{sub_color}; margin-top:10px;'>
กล้อง Pi Camera 3 ความละเอียด 1080p วิเคราะห์ภาพและพฤติกรรมอัตโนมัติ
</div>
</div>""", unsafe_allow_html=True)

    with row2_right:
        st.markdown(f"""<div class='info-card'>
<div style='font-weight:700; font-size:1.05rem; margin-bottom:12px;'>🎛️ สถานะอุปกรณ์ควบคุมอัตโนมัติ</div>
<div style='background:{metric_bg}; border-radius:10px; padding:12px 14px; margin-bottom:10px;'>
<div class='device-row'>
<span>💡 หลอดไฟกกความร้อน</span>
<span style='color:#f97316;'>ทำงาน 35%</span>
</div>
<div style='font-size:0.8rem; color:{sub_color}; margin-top:3px;'>ระบบหรี่ไฟ PID รักษาอุณหภูมิให้นิ่ง ไม่กระชากไฟ</div>
</div>
<div style='background:{metric_bg}; border-radius:10px; padding:12px 14px; margin-bottom:10px;'>
<div class='device-row'>
<span>🌀 พัดลมดูดระบายอากาศ</span>
<span style='color:#38bdf8;'>ต่อเนื่อง 20%</span>
</div>
<div style='font-size:0.8rem; color:{sub_color}; margin-top:3px;'>ระบายกลิ่นมูลและควบคุมความชื้นส่วนเกิน</div>
</div>
<div style='background:{metric_bg}; border-radius:10px; padding:12px 14px;'>
<div class='device-row'>
<span>⚡ อัตราการใช้ไฟเฉลี่ย</span>
<span style='color:#22c55e;'>38.5W (~4.1 บ./วัน)</span>
</div>
<div style='font-size:0.8rem; color:{sub_color}; margin-top:3px;'>ประหยัดไฟขึ้น 35% เทียบกับระบบตัดต่อธรรมดา</div>
</div>
</div>""", unsafe_allow_html=True)

    st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)

    # --- แถวที่ 3: ปุ่มลัดเลือกสูตรการกกสำเร็จรูป (1-Click Presets) ---
    st.markdown("##### 🌟 ปุ่มลัดเลือกสูตรการกกสำเร็จรูป (คลิกเดียว ปรับค่าเป้าหมายอัตโนมัติ)")
    p_col1, p_col2, p_col3 = st.columns(3)
    with p_col1:
        if st.button("🐣 แรกเกิด (1-3 วัน) • 36.5-37.2°C", use_container_width=True):
            user_pref.update({"temp_min": 36.5, "temp_max": 37.2, "hum_min": 55, "hum_max": 65})
            save_pref(user_pref)
            st.toast("✅ ปรับเป้าหมายเป็นสูตรลูกนกแรกเกิดสำเร็จ! (36.5 - 37.2 °C)")
            time.sleep(0.5)
            st.rerun()

    with p_col2:
        if st.button("🪶 แตกขนหนาม (4-7 วัน) • 34.0-35.5°C", use_container_width=True):
            user_pref.update({"temp_min": 34.0, "temp_max": 35.5, "hum_min": 50, "hum_max": 60})
            save_pref(user_pref)
            st.toast("✅ ปรับเป้าหมายเป็นสูตรแตกขนหนามสำเร็จ! (34.0 - 35.5 °C)")
            time.sleep(0.5)
            st.rerun()

    with p_col3:
        if st.button("🦜 ขนคลุมตัว (8+ วัน) • 31.0-33.0°C", use_container_width=True):
            user_pref.update({"temp_min": 31.0, "temp_max": 33.0, "hum_min": 45, "hum_max": 55})
            save_pref(user_pref)
            st.toast("✅ ปรับเป้าหมายเป็นสูตรขนคลุมตัวสำเร็จ! (31.0 - 33.0 °C)")
            time.sleep(0.5)
            st.rerun()

# --------------------------------------------------------------------------
# แท็บ 2: ประวัติและกราฟแนวโน้ม (ดึงสถิติจริงจาก SQLite ใน Pi 5)
# --------------------------------------------------------------------------
with tab_history:
    st.markdown("### 📈 กราฟประวัติสภาพแวดล้อมและการเจริญเติบโต")
    st.caption("ดึงสถิติจริงที่ Pi 5 บันทึกลงฐานข้อมูล SQLite ในตัว (brooder_data.db)")

    range_opt = st.radio(
        "เลือกช่วงเวลา:",
        ["24 ชั่วโมงล่าสุด", "7 วันย้อนหลัง", "30 วันย้อนหลัง"],
        horizontal=True
    )
    hours_lookup = {"24 ชั่วโมงล่าสุด": 24, "7 วันย้อนหลัง": 168, "30 วันย้อนหลัง": 720}
    records = db_manager.get_history(hours=hours_lookup.get(range_opt, 24))

    if records:
        t_stamps = [r["timestamp"][11:16] if "24" in range_opt else r["timestamp"][5:16] for r in records]
        temps = [r["temp"] for r in records]
        hums = [r["hum"] for r in records]
        weights = [r["weight"] for r in records]
        
        avg_t = sum(temps) / len(temps)
        avg_h = sum(hums) / len(hums)
        gain_w = round(weights[-1] - weights[0], 1)
        max_gas = max([r["nh3"] for r in records])
    else:
        t_stamps = [f"{h:02d}:00" for h in range(12, 24)]
        temps = [35.5, 35.6, 35.7, 35.6, 35.8, 35.7, 35.6, 35.6, 35.7, 35.6, 35.5, cur_t]
        hums = [57, 58, 59, 58, 58, 57, 58, 59, 58, 58, 57, cur_h]
        weights = [76.5, 76.8, 77.0, 77.3, 77.5, 77.8, 78.0, 78.2, 78.3, 78.4, 78.5, total_weight]
        avg_t, avg_h, gain_w, max_gas = cur_t, cur_h, 2.0, cur_nh3

    # กล่องสรุปสถิติตัวเลข 4 ช่อง (การ์ดสีขาวแยกสัดส่วนชัดเจน)
    stat_value_color = "#f1f5f9" if is_dark else "#333333"
    stat_cards = [
        ("🌡️", "อุณหภูมิเฉลี่ย", f"{avg_t:.1f}", "°C", None, "#f97316"),
        ("💧", "ความชื้นเฉลี่ย", f"{avg_h:.0f}", "%RH", None, "#0ea5e9"),
        ("⚖️", "น้ำหนักเพิ่มขึ้น", f"{'+' if gain_w >= 0 else ''}{gain_w}", "กรัม",
         ("↑ เติบโตดีเยี่ยม" if gain_w >= 0 else "↓ น้ำหนักลด", gain_w >= 0), "#a855f7"),
        ("💨", "แอมโมเนียสูงสุด", f"{max_gas}", "ppm",
         ("✓ ปลอดภัย" if max_gas < 10 else "⚠ เกินเกณฑ์", max_gas < 10), "#22c55e"),
    ]
    stat_cols = st.columns(4)
    for col, (icon, label, value, unit, badge, accent) in zip(stat_cols, stat_cards):
        badge_html = ""
        if badge:
            b_text, b_good = badge
            b_fg = "#15803d" if b_good else "#b91c1c"
            b_bg = "rgba(34,197,94,0.12)" if b_good else "rgba(239,68,68,0.12)"
            if is_dark:
                b_fg = "#4ade80" if b_good else "#f87171"
            badge_html = f"<span style='background:{b_bg}; color:{b_fg}; padding:3px 10px; border-radius:20px; font-size:0.78rem; font-weight:600;'>{b_text}</span>"
        col.markdown(f"""<div style='background:{card_bg}; border:1px solid {card_border}; border-left:4px solid {accent}; border-radius:14px; padding:16px 18px; min-height:118px; box-shadow:0 2px 10px rgba(15,23,42,{"0.25" if is_dark else "0.05"});'>
<div style='font-size:0.88rem; font-weight:600; color:{sub_color}; margin-bottom:6px;'>{icon} {label}</div>
<div style='font-size:1.9rem; font-weight:700; color:{stat_value_color}; line-height:1.15;'>{value}<span style='font-size:1rem; font-weight:500; color:{sub_color}; margin-left:4px;'>{unit}</span></div>
<div style='margin-top:8px; min-height:22px;'>{badge_html}</div>
</div>""", unsafe_allow_html=True)

    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)

    # สีกราฟให้อ่านง่าย ไม่กลืนพื้นหลัง
    chart_text = "#cbd5e1" if is_dark else "#374151"
    chart_grid = "rgba(148,163,184,0.18)" if is_dark else "#e5e7eb"
    chart_axis = "#475569" if is_dark else "#9ca3af"
    axis_style = dict(
        showgrid=True, gridcolor=chart_grid, gridwidth=1,
        showline=True, linecolor=chart_axis, linewidth=1,
        tickfont=dict(color=chart_text, size=12),
        title_font=dict(color=chart_text, size=13),
        zeroline=False,
    )
    chart_layout = dict(
        paper_bgcolor=card_bg,
        plot_bgcolor=card_bg,
        font=dict(family="Prompt, sans-serif", color=chart_text, size=12),
        title_font=dict(color=stat_value_color, size=15),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    font=dict(color=chart_text, size=12)),
        hoverlabel=dict(font_family="Prompt, sans-serif"),
    )

    # กราฟที่ 1: อุณหภูมิและความชื้น พร้อม Safe Zone
    fig1 = go.Figure()
    fig1.add_hrect(
        y0=t_min, y1=t_max,
        fillcolor="rgba(34, 197, 94, 0.15)",
        line_width=1,
        line_color="rgba(34, 197, 94, 0.4)",
        annotation_text="ช่วงอุณหภูมิปลอดภัย",
        annotation_position="top left",
        annotation_font_color="#22c55e" if is_dark else "#15803d"
    )
    fig1.add_trace(go.Scatter(
        x=t_stamps, y=temps, name="อุณหภูมิ (°C)",
        line=dict(color="#f97316", width=3, shape="spline"),
        marker=dict(size=6)
    ))
    fig1.add_trace(go.Scatter(
        x=t_stamps, y=hums, name="ความชื้น (%RH)", yaxis="y2",
        line=dict(color="#38bdf8", width=2.5, dash="dot", shape="spline"),
        marker=dict(size=5)
    ))
    fig1.update_layout(
        title="<b>กราฟอุณหภูมิและความชื้น</b>",
        height=340,
        margin=dict(l=50, r=50, t=60, b=40),
        xaxis=axis_style,
        yaxis=dict(axis_style, title="อุณหภูมิ (°C)", range=[32, 39]),
        yaxis2=dict(axis_style, title="ความชื้น (%RH)", range=[40, 80], overlaying="y", side="right", showgrid=False),
        **chart_layout
    )
    with st.container(border=True):
        st.plotly_chart(fig1, use_container_width=True, theme=None)

    # กราฟที่ 2: น้ำหนักรวมและการเจริญเติบโต
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=t_stamps, y=weights, name="น้ำหนักรวม (g)",
        fill="tozeroy",
        fillcolor="rgba(168, 85, 247, 0.15)",
        line=dict(color="#a855f7", width=3, shape="spline"),
        marker=dict(size=6)
    ))
    fig2.update_layout(
        title="<b>กราฟการเจริญเติบโตของน้ำหนักลูกนก (Weight Growth Curve)</b>",
        height=300,
        margin=dict(l=50, r=50, t=60, b=40),
        xaxis=axis_style,
        yaxis=dict(axis_style, title="น้ำหนัก (กรัม)"),
        **chart_layout
    )
    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)
    with st.container(border=True):
        st.plotly_chart(fig2, use_container_width=True, theme=None)

# --------------------------------------------------------------------------
# แท็บ 3: ห้องทดลอง AI Simulator (จำลองการทำงาน PID และการสั่งการตู้กก)
# --------------------------------------------------------------------------
with tab_sim:
    st.markdown("### 🧪 ห้องทดลองจำลองการทำงานสมองกล AI (PID Simulator)")
    st.caption("ทดสอบดูว่าเมื่ออุณหภูมิหรือความชื้นในตู้กกเปลี่ยนไป อัลกอริทึม AI จะคำนวณสั่งการฮีตเตอร์และพัดลมอัตโนมัติอย่างไร")

    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
    
    # ส่วนควบคุมสถานการณ์จำลอง
    sim_input_col1, sim_input_col2 = st.columns([1, 1], gap="large")
    
    with sim_input_col1:
        with st.container(border=True):
            st.markdown(f"""<div style='font-size:1.1rem; font-weight:700; color:#f97316; margin-bottom:12px; display:flex; align-items:center; gap:8px;'>
<span>🎛️</span> แผงควบคุมสภาพแวดล้อมจำลอง
</div>""", unsafe_allow_html=True)
            
            sim_t_val = st.slider(
                "🌡️ อุณหภูมิในตู้กกจำลอง (°C):",
                min_value=25.0, max_value=42.0,
                value=32.0,
                step=0.5,
                format="%.1f °C",
                help="ลองเลื่อนต่ำกว่า 34°C หรือสูงกว่า 37°C เพื่อดู AI สั่งการ"
            )
            
            sim_h_val = st.slider(
                "💧 ความชื้นในตู้กกจำลอง (%RH):",
                min_value=30, max_value=90,
                value=70,
                step=5,
                format="%d%%"
            )
            
            sim_target_mid = (t_min + t_max) / 2.0
            st.markdown(f"""
            <div style='background:rgba(249,115,22,0.08); border-radius:10px; padding:10px 14px; margin-top:8px; font-size:0.85rem; line-height:1.6;'>
                🎯 <b>เป้าหมายที่ตั้งไว้ในระบบ:</b> {t_min:.1f} - {t_max:.1f} °C (ค่ากลางเป้าหมาย: <b>{sim_target_mid:.1f} °C</b>)<br>
                📊 <b>ส่วนต่างอุณหภูมิ (Error):</b> <span style='color:{"#ef4444" if abs(sim_target_mid - sim_t_val) > 2 else "#22c55e"}; font-weight:700;'>
                {sim_t_val - sim_target_mid:+.1f} °C
                </span>
            </div>
            """, unsafe_allow_html=True)

    # คำนวณตรรกะ AI PID Controller
    error = sim_target_mid - sim_t_val  # ถ้าเป็นบวก = ตู้หนาวไป ต้องเร่งไฟ, ถ้าลบ = ตู้ร้อนเกิน ต้องดับไฟ
    
    # สัดส่วน PID P-term (Kp = 28)
    if error > 0:
        # หนาวเกินไป ต้องติดฮีตเตอร์
        calc_heater = min(100, int(35 + (error * 25)))
        heater_state = f"🔥 เร่งไฟกก {calc_heater}%"
        heater_color = "#ea580c"
    else:
        # อุณหภูมิพอดี หรือร้อนเกินไป
        calc_heater = max(0, int(20 + (error * 20)))
        heater_state = f"💡 ลดไฟกกเหลือ {calc_heater}%" if calc_heater > 0 else "🛑 ปิดไฟกก 0%"
        heater_color = "#f59e0b" if calc_heater > 0 else "#94a3b8"

    # พัดลม: ถ้าความชื้นสูงเกินเกณฑ์ หรือตู้ร้อนเกินไป ให้เร่งพัดลมระบาย
    if sim_t_val > t_max + 1.0 or sim_h_val > h_max:
        calc_fan = 80 if sim_t_val > t_max + 1.5 else 50
        fan_state = f"🌀 เร่งพัดลมระบาย {calc_fan}%"
        fan_color = "#ef4444" if sim_t_val > t_max + 1.5 else "#38bdf8"
        fan_reason = "ระบายความร้อนสะสม/ความชื้นส่วนเกิน"
    elif sim_t_val < t_min:
        calc_fan = 10  # หรี่พัดลมเบาสุดเพื่อกักเก็บความร้อน
        fan_state = "🌀 หรี่พัดลมเบา 10%"
        fan_color = "#64748b"
        fan_reason = "กักเก็บความอบอุ่น ไม่ให้ลูกนกหนาวสั่น"
    else:
        calc_fan = 20  # สปีดปกติหมุนเวียนอากาศ
        fan_state = "🌀 พัดลมหมุนเวียนปกติ 20%"
        fan_color = "#22c55e"
        fan_reason = "หมุนเวียนออกซิเจนบริสุทธิ์ต่อเนื่อง"

    with sim_input_col2:
        with st.container(border=True):
            st.markdown(f"""<div style='font-size:1.1rem; font-weight:700; color:#38bdf8; margin-bottom:12px; display:flex; align-items:center; gap:8px;'>
<span>🤖</span> การตัดสินใจและคำสั่งของ AI แบบ Real-time
</div>""", unsafe_allow_html=True)
            
            # การ์ดแสดงผลคำสั่งฮีตเตอร์
            st.markdown(f"""
            <div style='background:{card_bg}; border:1.5px solid {heater_color}; border-radius:12px; padding:12px 16px; margin-bottom:10px;'>
                <div style='display:flex; justify-content:space-between; align-items:center;'>
                    <span style='font-weight:700; font-size:1rem;'>💡 หลอดไฟกกความร้อน</span>
                    <span style='background:{heater_color}; color:#ffffff; padding:3px 12px; border-radius:12px; font-weight:700; font-size:0.85rem;'>{calc_heater}%</span>
                </div>
                <div style='font-size:0.85rem; color:{sub_color}; margin-top:4px;'>สถานะ: <b>{heater_state}</b></div>
            </div>
            
            <div style='background:{card_bg}; border:1.5px solid {fan_color}; border-radius:12px; padding:12px 16px; margin-bottom:10px;'>
                <div style='display:flex; justify-content:space-between; align-items:center;'>
                    <span style='font-weight:700; font-size:1rem;'>🌀 พัดลมดูดระบายอากาศ</span>
                    <span style='background:{fan_color}; color:#ffffff; padding:3px 12px; border-radius:12px; font-weight:700; font-size:0.85rem;'>{calc_fan}%</span>
                </div>
                <div style='font-size:0.85rem; color:{sub_color}; margin-top:4px;'>เหตุผล: {fan_reason}</div>
            </div>
            """, unsafe_allow_html=True)
            
            # JSON Payload ที่ส่งให้ Hardware จริง
            hardware_json = json.dumps({"heater": calc_heater, "fan": calc_fan, "mode": "PID_AUTO"}, indent=2)
            st.code(f"// คำสั่ง Serial JSON ส่งไปยังบอร์ดฮาร์ดแวร์จริง:\n{hardware_json}", language="json")

    st.markdown("<div style='height:15px;'></div>", unsafe_allow_html=True)

    # กราฟจำลองการปรับตัวของอุณหภูมิ (Response Curve Simulation)
    st.markdown("##### 📈 กราฟจำลองการตอบสนองของระบบเมื่อ AI เริ่มปรับจูน (PID Dynamic Curve)")
    
    # สร้างเส้นโค้งจำลอง 20 ขั้นเวลา (Time steps)
    time_steps = [f"{t}s" for t in range(0, 105, 5)]
    curve_temps = []
    curve_powers = []
    
    current_sim_t = sim_t_val
    for step in range(len(time_steps)):
        err = sim_target_mid - current_sim_t
        pwr = min(100, max(0, int(35 + (err * 22))))
        # อุณหภูมิค่อยๆ วิ่งเข้าหาเป้าหมายตามกำลังไฟ
        current_sim_t += (pwr - 35) * 0.04
        curve_temps.append(round(current_sim_t, 2))
        curve_powers.append(pwr)

    sim_fig = go.Figure()
    # ช่วงปลอดภัย
    sim_fig.add_hrect(
        y0=t_min, y1=t_max,
        fillcolor="rgba(34, 197, 94, 0.15)",
        line_width=1,
        line_color="rgba(34, 197, 94, 0.4)",
        annotation_text="ช่วงเป้าหมายสบายตัว",
        annotation_position="top left",
        annotation_font_color="#22c55e" if is_dark else "#15803d"
    )
    sim_fig.add_trace(go.Scatter(
        x=time_steps, y=curve_temps, name="อุณหภูมิตู้กก (°C)",
        line=dict(color="#f97316", width=3.5, shape="spline"),
        marker=dict(size=6)
    ))
    sim_fig.add_trace(go.Scatter(
        x=time_steps, y=curve_powers, name="กำลังไฟฮีตเตอร์ (%)", yaxis="y2",
        line=dict(color="#38bdf8", width=2, dash="dash", shape="spline"),
        marker=dict(size=4)
    ))

    sim_fig.update_layout(
        title="<b>แบบจำลองการไต่ระดับอุณหภูมิเข้าสู่จุดเสถียร (Thermal Equilibrium)</b>",
        height=320,
        margin=dict(l=50, r=50, t=50, b=40),
        xaxis=axis_style,
        yaxis=dict(axis_style, title="อุณหภูมิ (°C)", range=[min(24, min(curve_temps)-1), max(43, max(curve_temps)+1)]),
        yaxis2=dict(axis_style, title="กำลังไฟ (%)", range=[-5, 110], overlaying="y", side="right", showgrid=False),
        **chart_layout
    )
    with st.container(border=True):
        st.plotly_chart(sim_fig, use_container_width=True, theme=None)

# --------------------------------------------------------------------------
# แท็บ 4: บัญชี & การเชื่อมต่อ (Account & Connections Hub แบบ Discord Style)
# --------------------------------------------------------------------------
with tab_account:
    bot_tok = getattr(telegram_notifier, "DEFAULT_BOT_TOKEN", "8791062886:AAF6zx7kShfYne49bE-CF5MokneruSllkEY")
    is_logged_in = user_pref.get("account_logged_in", True)

    # 1. ส่วนบัญชีผู้ใช้งาน (User Account Header / ระบบเข้าสู่ระบบ & สมัครสมาชิก)
    with st.container(border=True, key="account_profile_card"):
        if not is_logged_in:
            st.markdown(f"""
            <div style='text-align:center; padding:8px 0 4px 0;'>
                <div style='font-size:2.4rem;'>🔐</div>
                <div style='font-size:1.35rem; font-weight:700; color:{text_color};'>ระบบบัญชีผู้ใช้งาน NestX ID</div>
                <div style='font-size:0.88rem; color:{sub_color}; margin-bottom:10px;'>
                    ข้อมูลการตั้งค่าตู้กกและบัญชีที่ผูกไว้ (Telegram, LINE, Discord, Gmail) จะถูกบันทึกแยกตามแต่ละบัญชีอย่างปลอดภัย
                </div>
            </div>
            """, unsafe_allow_html=True)

            auth_c1, auth_c2, auth_c3 = st.columns([0.8, 1.8, 0.8])
            with auth_c2:
                auth_mode = st.radio(
                    "เลือกโหมดบัญชี",
                    ["🔑 เข้าสู่ระบบ (Sign In)", "📝 สมัครบัญชีใหม่ (Sign Up)"],
                    horizontal=True,
                    label_visibility="collapsed"
                )
                st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

                if auth_mode == "🔑 เข้าสู่ระบบ (Sign In)":
                    in_user = st.text_input("ชื่อผู้ใช้ หรือ อีเมล (Username / Email):", placeholder="เช่น xuelihkl หรือ paritchayasueakla@gmail.com", key="login_user_input")
                    in_pass = st.text_input("รหัสผ่าน (Password):", type="password", placeholder="กรอกรหัสผ่านของคุณ", key="login_pass_input")
                    if st.button("🚀 เข้าสู่ระบบ (Sign In)", type="primary", use_container_width=True):
                        if not in_user.strip() or not in_pass:
                            st.warning("กรุณากรอกชื่อผู้ใช้/อีเมล และรหัสผ่านให้ครบครับ")
                        else:
                            ok, msg, acc_info = db_manager.authenticate_user(in_user, in_pass)
                            if ok and acc_info:
                                # โหลดค่าของบัญชีนั้นมาใช้งานทันที
                                saved_p = acc_info.get("preferences", {})
                                # รีเซ็ตค่าการเชื่อมต่อเดิมก่อน แล้วทับด้วยค่าของบัญชีที่ล็อกอิน
                                clean_acct_defaults = {
                                    "farm_name": saved_p.get("farm_name", f"ฟาร์มของ {acc_info['display_name']}"),
                                    "telegram_chat_id": saved_p.get("telegram_chat_id", ""),
                                    "telegram_username": saved_p.get("telegram_username", ""),
                                    "tg_alert_enabled": saved_p.get("tg_alert_enabled", True),
                                    "tg_daily_enabled": saved_p.get("tg_daily_enabled", True),
                                    "line_connected": saved_p.get("line_connected", False),
                                    "line_name": saved_p.get("line_name", ""),
                                    "line_token": saved_p.get("line_token", ""),
                                    "line_user_id": saved_p.get("line_user_id", ""),
                                    "discord_connected": saved_p.get("discord_connected", False),
                                    "discord_name": saved_p.get("discord_name", ""),
                                    "discord_webhook": saved_p.get("discord_webhook", ""),
                                    "email_connected": saved_p.get("email_connected", False),
                                    "email_address": saved_p.get("email_address", acc_info.get("email", "")),
                                    "temp_min": saved_p.get("temp_min", 35.0),
                                    "temp_max": saved_p.get("temp_max", 36.8),
                                    "hum_min": saved_p.get("hum_min", 50),
                                    "hum_max": saved_p.get("hum_max", 65),
                                    "account_logged_in": True,
                                    "account_username": acc_info["username"],
                                    "account_display_name": acc_info["display_name"],
                                    "account_email": acc_info.get("email", "")
                                }
                                user_pref.update(clean_acct_defaults)
                                save_pref(user_pref)
                                st.toast("✅ " + msg)
                                time.sleep(0.5)
                                st.rerun()
                            else:
                                st.error(msg)

                    # แสดงรายชื่อบัญชีที่มีในระบบเพื่อความสะดวกในการสลับทดสอบ
                    all_accs = db_manager.list_all_accounts()
                    if all_accs:
                        acc_names = ", ".join([f"<b>{a['username']}</b> ({a['display_name']})" for a in all_accs])
                        st.markdown(f"<div style='font-size:0.8rem; color:{sub_color}; margin-top:8px; text-align:center;'>📋 บัญชีที่ลงทะเบียนไว้แล้วในระบบ: {acc_names}</div>", unsafe_allow_html=True)

                else:
                    # โหมดสมัครบัญชีใหม่ (Sign Up)
                    reg_user = st.text_input("ตั้งชื่อผู้ใช้ (Username สำหรับล็อกอิน)*:", placeholder="เช่น farm_a หรือ somchai", key="reg_user_input")
                    reg_disp = st.text_input("ชื่อที่แสดง / ชื่อฟาร์ม (Display Name)*:", placeholder="เช่น ฟาร์มนกแก้วสมชาย", key="reg_disp_input")
                    reg_email = st.text_input("อีเมล (Email - ไม่บังคับ):", placeholder="เช่น somchai@gmail.com", key="reg_email_input")
                    reg_p1, reg_p2 = st.columns(2)
                    with reg_p1:
                        reg_pass = st.text_input("ตั้งรหัสผ่าน*:", type="password", placeholder="อย่างน้อย 4 ตัวอักษร", key="reg_pass1")
                    with reg_p2:
                        reg_pass_cf = st.text_input("ยืนยันรหัสผ่านอีกครั้ง*:", type="password", placeholder="กรอกรหัสผ่านซ้ำ", key="reg_pass2")

                    if st.button("✨ สมัครสมาชิกและเข้าใช้งานทันที", type="primary", use_container_width=True):
                        if not reg_user.strip() or not reg_pass:
                            st.warning("กรุณากรอก Username และรหัสผ่านให้ครบถ้วนครับ")
                        elif reg_pass != reg_pass_cf:
                            st.error("รหัสผ่านทั้ง 2 ช่องไม่ตรงกัน กรุณาตรวจสอบอีกครั้งครับ")
                        else:
                            disp_final = reg_disp.strip() or reg_user.strip()
                            init_account_prefs = {
                                "farm_name": disp_final,
                                "theme": user_pref.get("theme", "dark"),
                                "elec_rate": 4.5,
                                "temp_min": 35.0,
                                "temp_max": 36.8,
                                "hum_min": 50,
                                "hum_max": 65,
                                "telegram_chat_id": "",
                                "telegram_username": "",
                                "line_connected": False,
                                "discord_connected": False,
                                "email_connected": bool(reg_email.strip()),
                                "email_address": reg_email.strip(),
                                "account_username": reg_user.strip().lower(),
                                "account_display_name": disp_final,
                                "account_email": reg_email.strip()
                            }
                            ok, msg = db_manager.register_user(
                                username=reg_user,
                                password=reg_pass,
                                display_name=disp_final,
                                email=reg_email,
                                initial_prefs=init_account_prefs
                            )
                            if ok:
                                user_pref.update(init_account_prefs)
                                user_pref["account_logged_in"] = True
                                save_pref(user_pref)
                                st.toast("🎉 " + msg)
                                time.sleep(0.6)
                                st.rerun()
                            else:
                                st.error(msg)
        else:
            disp_show = html.escape(str(user_pref.get("account_display_name") or user_pref.get("account_username", "NestX Owner")))
            uname_show = html.escape(str(user_pref.get("account_username", "nestx_owner")))
            email_show = html.escape(str(user_pref.get("account_email") or user_pref.get("email_address", "")))
            safe_farm_show = html.escape(str(user_pref.get("farm_name", disp_show)))
            email_badge_html = f"<span style='font-size:0.82rem; color:{sub_color};'>({email_show})</span>" if email_show else ""
            acc_col1, acc_col2 = st.columns([3.2, 1.2])
            with acc_col1:
                st.markdown(f"""
                <div style='display:flex; align-items:center; gap:16px;'>
                    <div style='width:58px; height:58px; border-radius:50%; background:linear-gradient(135deg, #f97316, #ea580c); display:flex; align-items:center; justify-content:center; font-size:1.8rem; color:white; font-weight:700; box-shadow:0 4px 12px rgba(249,115,22,0.35);'>
                        🐣
                    </div>
                    <div>
                        <div style='display:flex; align-items:center; gap:8px; flex-wrap:wrap;'>
                            <span style='font-size:1.25rem; font-weight:700; color:{text_color};'>{disp_show}</span>
                            <span style='background:rgba(56,189,248,0.16); color:#38bdf8; padding:2px 10px; border-radius:12px; font-size:0.75rem; font-weight:700;'>@{uname_show}</span>
                            {email_badge_html}
                            <span style='background:rgba(34,197,94,0.18); color:#22c55e; padding:2px 10px; border-radius:12px; font-size:0.75rem; font-weight:700;'>🛡️ OWASP Protected & Cloud Synced</span>
                        </div>
                        <div style='font-size:0.86rem; color:{sub_color}; margin-top:3px;'>
                            ตู้กกประจำบัญชี: <b>{safe_farm_show}</b> &nbsp;•&nbsp; เข้ารหัส PBKDF2-SHA256 และซิงค์ข้อมูลออนไลน์บน Firebase Cloud เรียบร้อย 💾
                        </div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
            with acc_col2:
                if st.button("🔄 สลับบัญชี / ออกจากระบบ", use_container_width=True):
                    logout_current_user(user_pref)
                    st.toast("ออกจากระบบเรียบร้อย สามารถเข้าสู่ระบบบัญชีอื่นหรือสมัครใหม่ได้เลยครับ")
                    st.rerun()

    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)

    # 2. ส่วน "การเชื่อมต่อ" (Connections - Discord Style)
    st.markdown(f"""
    <div style='margin-bottom:14px;'>
        <div style='font-size:1.35rem; font-weight:700; color:{text_color};'>การเชื่อมต่อ</div>
        <div style='font-size:0.9rem; color:{sub_color}; margin-top:2px;'>
            เชื่อมต่อบัญชีแพลตฟอร์มแชตของคุณกับตู้กก NestX เพื่อปลดล็อกการแจ้งเตือนฉุกเฉินอัตโนมัติ รายงานสรุปประจำวัน และการส่งภาพถ่ายลูกนกแบบเรียลไทม์
        </div>
    </div>
    """, unsafe_allow_html=True)

    # แถบ "เพิ่มการเชื่อมต่อใหม่" (Add New Connection Icon Tile Bar)
    st.markdown(f"<div style='font-size:1rem; font-weight:700; color:{text_color}; margin-bottom:10px;'>เพิ่มการเชื่อมต่อใหม่</div>", unsafe_allow_html=True)

    if "selected_connect_platform" not in st.session_state:
        st.session_state.selected_connect_platform = None

    p_btn1, p_btn2, p_btn3, p_btn4, _spacer = st.columns([1.1, 1.1, 1.1, 1.1, 2])
    with p_btn1:
        if st.button("✈️ Telegram", use_container_width=True, help="เชื่อมต่อบอท Telegram (@TheNestXBot)"):
            st.session_state.selected_connect_platform = "telegram"
    with p_btn2:
        if st.button("💬 LINE", use_container_width=True, help="เชื่อมต่อการแจ้งเตือนผ่าน LINE"):
            st.session_state.selected_connect_platform = "line"
    with p_btn3:
        if st.button("🎮 Discord", use_container_width=True, help="เชื่อมต่อผ่าน Discord Webhook"):
            st.session_state.selected_connect_platform = "discord"
    with p_btn4:
        if st.button("✉️ Gmail", use_container_width=True, help="เชื่อมต่ออีเมลรับรายงานประจำวัน"):
            st.session_state.selected_connect_platform = "email"

    # กล่อง Pop-up สำหรับผูกบัญชีเมื่อคลิกเลือกไอคอนแพลตฟอร์ม
    active_plat = st.session_state.selected_connect_platform
    if active_plat == "telegram":
        with st.container(border=True, key="connect_modal_tg"):
            if "tg_pair_code" not in st.session_state:
                st.session_state.tg_pair_code = f"nest{np.random.randint(1000, 9999)}"
            pair_code = st.session_state.tg_pair_code
            deep_link = f"https://t.me/TheNestXBot?start={pair_code}"

            st.markdown(f"""
            <div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;'>
                <div style='font-size:1.1rem; font-weight:700; color:#38bdf8;'>✈️ เชื่อมต่อบัญชี Telegram (@TheNestXBot)</div>
            </div>
            <div style='font-size:0.88rem; color:{sub_color}; margin-bottom:14px;'>
                แตะปุ่มที่ 1 เพื่อเปิดแอป Telegram แล้วกด <b>Start</b> จากนั้นกดปุ่มที่ 2 เพื่อซิงค์บัญชีเข้ากับโปรไฟล์ของคุณทันที
            </div>
            <div style='margin-bottom:14px;'>
                <a href="{deep_link}" target="_blank" style="text-decoration:none;">
                    <span style="background:#0284c7; color:#ffffff; padding:10px 20px; border-radius:10px; font-weight:700; font-size:0.9rem; display:inline-flex; align-items:center; gap:8px;">
                        🚀 1. เปิดแอป Telegram & กด START
                    </span>
                </a>
            </div>
            """, unsafe_allow_html=True)

            m_c1, m_c2 = st.columns([2, 1])
            with m_c1:
                if st.button("🔄 2. ยืนยันการเชื่อมต่ออัตโนมัติ", type="primary", use_container_width=True):
                    importlib.reload(telegram_notifier)
                    det_cid, det_name = telegram_notifier.check_for_connection_link(expected_token=pair_code, bot_token=bot_tok)
                    if not det_cid:
                        det_cid, det_name = telegram_notifier.check_for_connection_link(expected_token=None, bot_token=bot_tok)
                    if det_cid:
                        user_pref["telegram_chat_id"] = det_cid
                        user_pref["telegram_username"] = det_name or "Telegram User"
                        user_pref["telegram_token"] = bot_tok
                        save_pref(user_pref)
                        st.session_state.selected_connect_platform = None
                        st.toast(f"🎉 เชื่อมต่อ Telegram ({det_name}) สำเร็จ!")
                        st.rerun()
                    else:
                        st.error("ยังไม่พบการกด Start จาก Telegram กรุณากด Start ในบอทก่อนครับ")
            with m_c2:
                if st.button("✖ ปิดหน้าต่างนี้", use_container_width=True):
                    st.session_state.selected_connect_platform = None
                    st.rerun()

    elif active_plat == "line":
        with st.container(border=True, key="connect_modal_line"):
            st.markdown("<div style='font-size:1.1rem; font-weight:700; color:#22c55e; margin-bottom:8px;'>💬 เชื่อมต่อบัญชี LINE Official / Notify</div>", unsafe_allow_html=True)
            l_name = st.text_input("ชื่อบัญชี LINE ของคุณ (ใช้แสดงบนโปรไฟล์):", value=user_pref.get("line_name", "") or "LINE User", placeholder="เช่น Pla Ping")
            l_tok = st.text_input("LINE Channel Access Token (ถ้ามี):", value=user_pref.get("line_token", ""), type="password", placeholder="วาง Token หรือเว้นว่างเพื่อเชื่อมแบบโปรไฟล์จำลอง")
            l_uid = st.text_input("LINE User ID (ถ้ามี):", value=user_pref.get("line_user_id", ""), placeholder="เช่น U1234567890abcdef...")
            lc1, lc2 = st.columns([2, 1])
            with lc1:
                if st.button("✅ บันทึกการเชื่อมต่อ LINE", type="primary", use_container_width=True):
                    user_pref["line_connected"] = True
                    user_pref["line_name"] = l_name.strip() or "LINE User"
                    user_pref["line_token"] = l_tok.strip()
                    user_pref["line_user_id"] = l_uid.strip()
                    save_pref(user_pref)
                    st.session_state.selected_connect_platform = None
                    st.toast("🎉 เชื่อมต่อบัญชี LINE สำเร็จ!")
                    st.rerun()
            with lc2:
                if st.button("✖ ยกเลิก", use_container_width=True, key="cancel_line"):
                    st.session_state.selected_connect_platform = None
                    st.rerun()

    elif active_plat == "discord":
        with st.container(border=True, key="connect_modal_discord"):
            st.markdown("<div style='font-size:1.1rem; font-weight:700; color:#818cf8; margin-bottom:8px;'>🎮 เชื่อมต่อ Discord Server / Webhook</div>", unsafe_allow_html=True)
            d_name = st.text_input("ชื่อบัญชี / ชื่อช่อง Discord:", value=user_pref.get("discord_name", "") or "NestX Alert Channel")
            d_url = st.text_input("Discord Webhook URL:", value=user_pref.get("discord_webhook", ""), placeholder="https://discord.com/api/webhooks/...")
            dc1, dc2 = st.columns([2, 1])
            with dc1:
                if st.button("✅ เชื่อมต่อ Discord", type="primary", use_container_width=True):
                    user_pref["discord_connected"] = True
                    user_pref["discord_name"] = d_name.strip() or "Discord User"
                    user_pref["discord_webhook"] = d_url.strip()
                    save_pref(user_pref)
                    st.session_state.selected_connect_platform = None
                    st.toast("🎉 เชื่อมต่อ Discord สำเร็จ!")
                    st.rerun()
            with dc2:
                if st.button("✖ ยกเลิก", use_container_width=True, key="cancel_dc"):
                    st.session_state.selected_connect_platform = None
                    st.rerun()

    elif active_plat == "email":
        with st.container(border=True, key="connect_modal_email"):
            st.markdown("<div style='font-size:1.1rem; font-weight:700; color:#f43f5e; margin-bottom:8px;'>✉️ เชื่อมต่อ Gmail สำหรับรับรายงานประจำวัน</div>", unsafe_allow_html=True)
            e_addr = st.text_input("อีเมลของคุณ (Gmail):", value=user_pref.get("email_address", ""), placeholder="example@gmail.com")
            ec1, ec2 = st.columns([2, 1])
            with ec1:
                if st.button("✅ ผูกบัญชีอีเมล", type="primary", use_container_width=True):
                    if e_addr.strip():
                        user_pref["email_connected"] = True
                        user_pref["email_address"] = e_addr.strip()
                        save_pref(user_pref)
                        st.session_state.selected_connect_platform = None
                        st.toast("🎉 เชื่อมต่ออีเมลสำเร็จ!")
                        st.rerun()
                    else:
                        st.warning("กรุณากรอกอีเมลก่อนครับ")
            with ec2:
                if st.button("✖ ยกเลิก", use_container_width=True, key="cancel_em"):
                    st.session_state.selected_connect_platform = None
                    st.rerun()

    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)

    # 3. รายการบัญชีที่เชื่อมต่อแล้ว (Connected Accounts Cards แบบ Discord)
    tg_cid = str(user_pref.get("telegram_chat_id", "")).strip()
    tg_connected = bool(tg_cid)
    line_connected = bool(user_pref.get("line_connected", False))
    discord_connected = bool(user_pref.get("discord_connected", False))
    email_connected = bool(user_pref.get("email_connected", False))

    connected_count = sum([tg_connected, line_connected, discord_connected, email_connected])
    st.markdown(f"<div style='font-size:1.05rem; font-weight:700; color:{text_color}; margin-bottom:12px;'>การเชื่อมต่อ {connected_count} รายการ</div>", unsafe_allow_html=True)

    if connected_count == 0:
        st.markdown(f"""
        <div style='background:{card_bg}; border:1.5px dashed {card_border}; border-radius:16px; padding:32px; text-align:center; color:{sub_color};'>
            <div style='font-size:2.2rem; margin-bottom:8px;'>🔌</div>
            <div style='font-weight:600; font-size:1rem; color:{text_color};'>ยังไม่มีบัญชีที่เชื่อมต่อ</div>
            <div style='font-size:0.86rem; margin-top:4px;'>กดเลือกไอคอนแพลตฟอร์มด้านบน (เช่น ✈️ Telegram หรือ 💬 LINE) เพื่อเริ่มผูกบัญชีรับแจ้งเตือน</div>
        </div>
        """, unsafe_allow_html=True)

    # --- การ์ดที่ 1: Telegram (ถ้าเชื่อมต่อแล้ว) ---
    if tg_connected:
        tg_disp_name = user_pref.get("telegram_username") or "บัญชีผู้ใช้ Telegram"
        with st.container(border=True, key="conn_card_tg"):
            top_c1, top_c2, top_c3 = st.columns([4, 1.3, 0.5])
            with top_c1:
                st.markdown(f"""
                <div style='display:flex; align-items:center; gap:12px;'>
                    <div style='width:42px; height:42px; border-radius:50%; background:#0284c7; display:flex; align-items:center; justify-content:center; font-size:1.3rem; color:white;'>
                        ✈️
                    </div>
                    <div>
                        <div style='font-size:1.08rem; font-weight:700; color:{text_color}; line-height:1.2;'>{tg_disp_name}</div>
                        <div style='font-size:0.82rem; color:#38bdf8; font-weight:600;'>Telegram (@TheNestXBot) • เชื่อมต่อแล้ว</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
            with top_c2:
                if st.button("🔔 ส่งทดสอบ", key="btn_test_tg_card", use_container_width=True):
                    ok, msg = telegram_notifier.test_telegram_connection(bot_tok, tg_cid)
                    if ok:
                        st.toast("✅ ส่งข้อความเข้า Telegram สำเร็จ!")
                    else:
                        st.error(msg)
            with top_c3:
                if st.button("✕", key="btn_disc_tg", help="ยกเลิกการเชื่อมต่อ Telegram", use_container_width=True):
                    user_pref["telegram_chat_id"] = ""
                    user_pref["telegram_username"] = ""
                    save_pref(user_pref)
                    st.toast("ยกเลิกการเชื่อมต่อ Telegram แล้ว")
                    st.rerun()

            st.markdown("<hr style='margin:10px 0; border-color:rgba(148,163,184,0.18);'>", unsafe_allow_html=True)
            t_sw1 = st.toggle("🚨 รับแจ้งเตือนวิกฤตฉุกเฉินทันที (ฮีตเตอร์เสีย / ร้อนจัด / ไฟดับ)", value=user_pref.get("tg_alert_enabled", True), key="sw_tg_alert")
            t_sw2 = st.toggle("📊 รับสรุปรายงานสุขภาพประจำวันอัตโนมัติ (19:00 น.)", value=user_pref.get("tg_daily_enabled", True), key="sw_tg_daily")
            if t_sw1 != user_pref.get("tg_alert_enabled", True) or t_sw2 != user_pref.get("tg_daily_enabled", True):
                user_pref["tg_alert_enabled"] = t_sw1
                user_pref["tg_daily_enabled"] = t_sw2
                save_pref(user_pref)

    # --- การ์ดที่ 2: LINE (ถ้าเชื่อมต่อแล้ว) ---
    if line_connected:
        st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
        line_disp_name = user_pref.get("line_name") or "LINE User"
        with st.container(border=True, key="conn_card_line"):
            l_top1, l_top2, l_top3 = st.columns([4, 1.3, 0.5])
            with l_top1:
                st.markdown(f"""
                <div style='display:flex; align-items:center; gap:12px;'>
                    <div style='width:42px; height:42px; border-radius:50%; background:#06c755; display:flex; align-items:center; justify-content:center; font-size:1.3rem; color:white;'>
                        💬
                    </div>
                    <div>
                        <div style='font-size:1.08rem; font-weight:700; color:{text_color}; line-height:1.2;'>{line_disp_name}</div>
                        <div style='font-size:0.82rem; color:#22c55e; font-weight:600;'>LINE Official Notification • เชื่อมต่อแล้ว</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
            with l_top2:
                if st.button("🔔 ส่งทดสอบ", key="btn_test_line_card", use_container_width=True):
                    if user_pref.get("line_token") and user_pref.get("line_user_id"):
                        ok, msg = telegram_notifier.send_line_message(
                            user_pref.get("line_token"), user_pref.get("line_user_id"),
                            "🐣 ทดสอบการแจ้งเตือนจากตู้กกอัจฉริยะ NestX สำเร็จ!"
                        )
                        if ok:
                            st.toast("✅ " + msg)
                        else:
                            st.error(msg)
                    else:
                        st.toast("✅ ทดสอบส่งแจ้งเตือนไปยังบัญชี LINE (" + line_disp_name + ") เรียบร้อย!")
            with l_top3:
                if st.button("✕", key="btn_disc_line", help="ยกเลิกการเชื่อมต่อ LINE", use_container_width=True):
                    user_pref["line_connected"] = False
                    save_pref(user_pref)
                    st.toast("ยกเลิกการเชื่อมต่อ LINE แล้ว")
                    st.rerun()

            st.markdown("<hr style='margin:10px 0; border-color:rgba(148,163,184,0.18);'>", unsafe_allow_html=True)
            l_sw1 = st.toggle("🚨 รับแจ้งเตือนวิกฤตฉุกเฉินผ่าน LINE", value=user_pref.get("line_alert_enabled", True), key="sw_line_alert")
            l_sw2 = st.toggle("📊 รับสรุปรายงานสุขภาพประจำวันผ่าน LINE", value=user_pref.get("line_daily_enabled", True), key="sw_line_daily")
            if l_sw1 != user_pref.get("line_alert_enabled", True) or l_sw2 != user_pref.get("line_daily_enabled", True):
                user_pref["line_alert_enabled"] = l_sw1
                user_pref["line_daily_enabled"] = l_sw2
                save_pref(user_pref)

    # --- การ์ดที่ 3: Discord (ถ้าเชื่อมต่อแล้ว) ---
    if discord_connected:
        st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
        dc_disp_name = user_pref.get("discord_name") or "Discord Server"
        with st.container(border=True, key="conn_card_dc"):
            d_top1, d_top2, d_top3 = st.columns([4, 1.3, 0.5])
            with d_top1:
                st.markdown(f"""
                <div style='display:flex; align-items:center; gap:12px;'>
                    <div style='width:42px; height:42px; border-radius:50%; background:#5865F2; display:flex; align-items:center; justify-content:center; font-size:1.3rem; color:white;'>
                        🎮
                    </div>
                    <div>
                        <div style='font-size:1.08rem; font-weight:700; color:{text_color}; line-height:1.2;'>{dc_disp_name}</div>
                        <div style='font-size:0.82rem; color:#818cf8; font-weight:600;'>Discord Webhook • เชื่อมต่อแล้ว</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
            with d_top2:
                if st.button("🔔 ส่งทดสอบ", key="btn_test_dc_card", use_container_width=True):
                    if user_pref.get("discord_webhook"):
                        ok, msg = telegram_notifier.send_discord_webhook(
                            user_pref.get("discord_webhook"),
                            "🐣 **NestX Smart Brooder:** ทดสอบการเชื่อมต่อแจ้งเตือนเข้า Discord สำเร็จ! ✅"
                        )
                        if ok:
                            st.toast("✅ " + msg)
                        else:
                            st.error(msg)
                    else:
                        st.toast("✅ ทดสอบส่งแจ้งเตือนไปยัง Discord (" + dc_disp_name + ") สำเร็จ!")
            with d_top3:
                if st.button("✕", key="btn_disc_dc", help="ยกเลิกการเชื่อมต่อ Discord", use_container_width=True):
                    user_pref["discord_connected"] = False
                    save_pref(user_pref)
                    st.toast("ยกเลิกการเชื่อมต่อ Discord แล้ว")
                    st.rerun()

            st.markdown("<hr style='margin:10px 0; border-color:rgba(148,163,184,0.18);'>", unsafe_allow_html=True)
            d_sw1 = st.toggle("🚨 แจ้งเตือนฉุกเฉินเข้าช่องแชต Discord", value=user_pref.get("discord_alert_enabled", True), key="sw_dc_alert")
            d_sw2 = st.toggle("📊 ส่งกราฟและสรุปรายงานประจำวันเข้า Discord", value=user_pref.get("discord_daily_enabled", True), key="sw_dc_daily")
            if d_sw1 != user_pref.get("discord_alert_enabled", True) or d_sw2 != user_pref.get("discord_daily_enabled", True):
                user_pref["discord_alert_enabled"] = d_sw1
                user_pref["discord_daily_enabled"] = d_sw2
                save_pref(user_pref)

    # --- การ์ดที่ 4: Email / Gmail (ถ้าเชื่อมต่อแล้ว) ---
    if email_connected:
        st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
        em_addr = user_pref.get("email_address") or "owner@nestx.ai"
        with st.container(border=True, key="conn_card_email"):
            e_top1, e_top2 = st.columns([5.3, 0.5])
            with e_top1:
                st.markdown(f"""
                <div style='display:flex; align-items:center; gap:12px;'>
                    <div style='width:42px; height:42px; border-radius:50%; background:#ea4335; display:flex; align-items:center; justify-content:center; font-size:1.3rem; color:white;'>
                        ✉️
                    </div>
                    <div>
                        <div style='font-size:1.08rem; font-weight:700; color:{text_color}; line-height:1.2;'>{em_addr}</div>
                        <div style='font-size:0.82rem; color:#f43f5e; font-weight:600;'>Gmail Daily Report • เชื่อมต่อแล้ว</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
            with e_top2:
                if st.button("✕", key="btn_disc_em", help="ยกเลิกการเชื่อมต่ออีเมล", use_container_width=True):
                    user_pref["email_connected"] = False
                    save_pref(user_pref)
                    st.toast("ยกเลิกการเชื่อมต่ออีเมลแล้ว")
                    st.rerun()

            st.markdown("<hr style='margin:10px 0; border-color:rgba(148,163,184,0.18);'>", unsafe_allow_html=True)
            e_sw1 = st.toggle("📩 ส่งไฟล์สรุปสุขภาพลูกนกประจำสัปดาห์ทางอีเมล", value=user_pref.get("email_daily_enabled", True), key="sw_em_daily")
            if e_sw1 != user_pref.get("email_daily_enabled", True):
                user_pref["email_daily_enabled"] = e_sw1
                save_pref(user_pref)

# --------------------------------------------------------------------------
# แท็บ 5: ตั้งค่าระบบ & รายงาน (Settings & Telegram Photo & PDF Export)
# --------------------------------------------------------------------------
with tab_settings:
    st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
    
    set_col1, set_col2 = st.columns([1, 1], gap="large")

    with set_col1:
        with st.container(border=True, key="env_settings_card"):
            st.markdown("""<div style='font-size:1.18rem; font-weight:700; color:#ea580c; margin-bottom:14px; display:flex; align-items:center; gap:8px;'>
<span>🎯</span> ตั้งค่าเป้าหมายสภาพแวดล้อม
</div>""", unsafe_allow_html=True)

            t_range = st.slider(
                "🌡️ ช่วงอุณหภูมิเป้าหมาย (°C):",
                min_value=30.0, max_value=40.0,
                value=(float(t_min), float(t_max)),
                step=0.5,
                format="%.1f °C"
            )
            st.caption(f"💡 ระบบ PID จะคุมความร้อนให้อยู่ระหว่าง **{t_range[0]:.1f} °C** ถึง **{t_range[1]:.1f} °C**")

            st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

            h_range = st.slider(
                "💧 ช่วงความชื้นเป้าหมาย (%RH):",
                min_value=30, max_value=85,
                value=(int(h_min), int(h_max)),
                step=5,
                format="%d%%"
            )
            st.caption(f"💡 ระบบจะคุมความชื้นให้อยู่ระหว่าง **{h_range[0]}%** ถึง **{h_range[1]}%**")

            st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)

            new_rate = st.slider(
                "⚡ อัตราค่าไฟฟ้า (บาทต่อหน่วย kWh):",
                min_value=2.0, max_value=8.0,
                value=float(user_pref.get("elec_rate", 4.5)),
                step=0.25,
                format="%.2f บาท"
            )

            st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)
            if st.button("💾 บันทึกค่าเป้าหมายสภาพแวดล้อม", type="primary", use_container_width=True):
                user_pref.update({
                    "temp_min": t_range[0],
                    "temp_max": t_range[1],
                    "hum_min": h_range[0],
                    "hum_max": h_range[1],
                    "elec_rate": new_rate
                })
                save_pref(user_pref)
                st.toast("✅ บันทึกค่าเป้าหมายเรียบร้อยแล้ว!")
                st.rerun()

    with set_col2:
        with st.container(border=True, key="telegram_settings_card"):
            bot_tok = getattr(telegram_notifier, "DEFAULT_BOT_TOKEN", "8791062886:AAF6zx7kShfYne49bE-CF5MokneruSllkEY")
            chat_id = str(user_pref.get("telegram_chat_id", "")).strip()
            is_tg_connected = bool(chat_id)
            line_conn = bool(user_pref.get("line_connected", False))
            dc_conn = bool(user_pref.get("discord_connected", False))
            total_conn = sum([is_tg_connected, line_conn, dc_conn])

            st.markdown(f"""<div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;'>
<div style='font-size:1.15rem; font-weight:700; color:#38bdf8; display:flex; align-items:center; gap:8px;'>
<span>🔔</span> ศูนย์ทดสอบระบบแจ้งเตือนอัตโนมัติ
</div>
<span style='background:rgba(34,197,94,0.18); color:#22c55e; padding:3px 12px; border-radius:12px; font-size:0.8rem; font-weight:700;'>เชื่อมแล้ว {total_conn} ช่องทาง</span>
</div>""", unsafe_allow_html=True)

            st.markdown(f"""
            <div style='font-size:0.88rem; color:{sub_color}; margin-bottom:12px;'>
                สามารถผูกหรือสลับบัญชี <b>Telegram, LINE, Discord, Gmail</b> ได้ที่แท็บ <b>"👤 บัญชี & การเชื่อมต่อ"</b> ด้านบน
            </div>
            """, unsafe_allow_html=True)

            # ปุ่มหลัก: ส่งทดสอบ & ส่งสรุปรายงานวันนี้
            tg_b1, tg_b2 = st.columns(2)
            with tg_b1:
                if st.button("🔔 ส่งทดสอบ", use_container_width=True, help="ส่งข้อความทดสอบการเชื่อมต่อไปยัง Telegram"):
                    if chat_id:
                        ok, msg = telegram_notifier.test_telegram_connection(bot_tok, chat_id)
                        if ok:
                            st.toast("✅ ส่งข้อความทดสอบเข้ามือถือคุณสำเร็จ!")
                        else:
                            st.error(msg)
                    else:
                        st.warning("กรุณาผูกบัญชี Telegram ในแท็บ '👤 บัญชี & การเชื่อมต่อ' ก่อนครับ")

            with tg_b2:
                if st.button("📊 ส่งสรุปวันนี้", use_container_width=True, help="ส่งรายงานสรุปประจำวันเข้า Telegram"):
                    if chat_id:
                        stat = db_manager.get_daily_summary()
                        ok, msg = telegram_notifier.send_daily_summary(
                            stat, farm_name=user_pref.get("farm_name", "NestX"),
                            elec_rate=new_rate, bot_token=bot_tok, chat_id=chat_id
                        )
                        if ok:
                            st.toast("✅ ส่งรายงานสรุปสำเร็จ!")
                        else:
                            st.error(msg)
                    else:
                        st.warning("กรุณาผูกบัญชี Telegram ในแท็บ '👤 บัญชี & การเชื่อมต่อ' ก่อนครับ")

            st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)

            st.markdown(f"""<div style='background:rgba(30, 41, 59, 0.7); border:1px solid rgba(56, 189, 248, 0.25); border-radius:12px; padding:12px 16px; margin-bottom:14px; font-size:0.88rem; line-height:1.7;'>
<div style='margin-bottom:8px;'>
    <div><span style='color:#38bdf8; font-weight:700;'>⏰ รายงานประจำวัน:</span> <span style='color:#f1f5f9;'>สรุปสถิติส่งอัตโนมัติ 19:00 น.</span></div>
</div>
<div>
    <div><span style='color:#f87171; font-weight:700;'>🚨 แจ้งเตือนฉุกเฉิน:</span> <span style='color:#f1f5f9;'>ยิงเตือนทันทีเมื่อฮีตเตอร์เสีย/ร้อนจัด</span></div>
</div>
</div>""", unsafe_allow_html=True)

            with st.expander("🧪 ทดสอบจำลอง 2 ระบบนี้ส่งเข้า Telegram ทันที"):
                sim_col1, sim_col2 = st.columns(2)
                with sim_col1:
                    if st.button("⏰ เทสส่ง: รายงานประจำวัน", use_container_width=True):
                        if chat_id:
                            stat = db_manager.get_daily_summary()
                            ok, msg = telegram_notifier.send_daily_summary(
                                stat, farm_name=user_pref.get("farm_name", "NestX"),
                                elec_rate=new_rate, bot_token=bot_tok, chat_id=chat_id
                            )
                            if ok:
                                st.toast("✅ ส่งรายงานประจำวันเข้า Telegram แล้ว!")
                            else:
                                st.error(msg)
                        else:
                            st.warning("กรุณาผูกบัญชี Telegram ในแท็บ '👤 บัญชี & การเชื่อมต่อ' ก่อนครับ")

                with sim_col2:
                    if st.button("🚨 เทสส่ง: เตือนฉุกเฉิน (ร้อนจัด)", use_container_width=True):
                        if chat_id:
                            ok, msg = telegram_notifier.send_emergency_alert(
                                "OVERHEAT", current_val=39.5, target_val=36.5,
                                bot_token=bot_tok, chat_id=chat_id, with_photo=False,
                                bypass_cooldown=True
                            )
                            if ok:
                                st.toast("🚨 ส่งข้อความแจ้งเตือนฉุกเฉินแล้ว!")
                            else:
                                st.error(msg)
                        else:
                            st.warning("กรุณาผูกบัญชี Telegram ในแท็บ '👤 บัญชี & การเชื่อมต่อ' ก่อนครับ")

    # ==========================================
    # 📸 การ์ดส่งรูป + ข้อความเข้า Telegram
    # ==========================================
    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)
    with st.container(border=True, key="photo_send_card"):
        st.markdown(f"""<div style='font-size:1.15rem; font-weight:700; color:{'#38bdf8' if is_dark else '#0284c7'}; display:flex; align-items:center; gap:8px;'>
<span>📸</span> ส่งรูปและข้อความเข้า Telegram
</div>
<div style='font-size:0.85rem; color:{sub_color}; margin:4px 0 14px 0;'>
ถ่ายจากกล้องในตู้กก หรือเลือกรูปจากเครื่อง แล้วส่งเข้ามือถือพร้อมข้อความ
</div>""", unsafe_allow_html=True)

        photo_col1, photo_col2 = st.columns([1, 1], gap="large")

        with photo_col1:
            photo_source = st.radio(
                "แหล่งรูปภาพ",
                ["📷 กล้องในตู้กก", "🖼️ เลือกรูปจากเครื่อง"],
                horizontal=True,
                label_visibility="collapsed"
            )

            if "tg_snapshot" not in st.session_state:
                st.session_state.tg_snapshot = None

            photo_bytes = None
            if photo_source == "📷 กล้องในตู้กก":
                if st.button("📷 ถ่ายรูปตอนนี้", use_container_width=True):
                    with st.spinner("กำลังถ่ายรูป..."):
                        st.session_state.tg_snapshot = telegram_notifier.capture_camera_image()
                    if st.session_state.tg_snapshot is None:
                        st.warning("ไม่พบกล้อง — ต่อ Pi Camera หรือ USB Webcam ก่อน หรือเลือกรูปจากเครื่องแทน")
                photo_bytes = st.session_state.tg_snapshot
            else:
                uploaded = st.file_uploader(
                    "เลือกรูปภาพ",
                    type=["jpg", "jpeg", "png"],
                    label_visibility="collapsed"
                )
                if uploaded is not None:
                    photo_bytes = uploaded.getvalue()

            if photo_bytes:
                st.image(photo_bytes, width="stretch")

        with photo_col2:
            attach_stats = st.toggle("📊 แนบค่าเซ็นเซอร์ปัจจุบัน", value=True)
            user_caption = st.text_area(
                "ข้อความ",
                placeholder="เช่น วันนี้น้องกินเก่งมาก 🐥",
                height=110,
                max_chars=600
            )

            # ประกอบข้อความใต้รูป
            caption_parts = []
            if user_caption.strip():
                caption_parts.append(html.escape(user_caption.strip()))
            if attach_stats:
                caption_parts.append(
                    f"━━━━━━━━━━━━━━\n"
                    f"🌡️ อุณหภูมิ: <b>{cur_t:.1f}°C</b>\n"
                    f"💧 ความชื้น: <b>{cur_h:.0f}%</b>\n"
                    f"💨 แอมโมเนีย: <b>{cur_nh3} ppm</b>\n"
                    f"⚖️ น้ำหนักรวม: <b>{total_weight:.1f} g</b>\n"
                    f"⏰ {datetime.now().strftime('%H:%M  %d/%m/%Y')}"
                )
            final_caption = "\n".join(caption_parts)

            send_label = "🚀 ส่งรูป + ข้อความ" if photo_bytes else "🚀 ส่งข้อความ"
            if st.button(send_label, type="primary", use_container_width=True):
                if not chat_id:
                    st.warning("กรุณากรอก Chat ID ด้านบนก่อนครับ")
                elif not photo_bytes and not final_caption:
                    st.warning("ยังไม่มีรูปหรือข้อความให้ส่ง")
                else:
                    with st.spinner("กำลังส่ง..."):
                        if photo_bytes:
                            ok, msg = telegram_notifier.send_telegram_photo(photo_bytes, final_caption, bot_tok, chat_id)
                        else:
                            ok, msg = telegram_notifier.send_telegram(final_caption, bot_tok, chat_id)
                    if ok:
                        st.toast("✅ " + msg)
                    else:
                        st.error(msg)

    # การ์ดออกรายงานสุขภาพส่งตรวจ
    st.markdown("<div style='height:18px;'></div>", unsafe_allow_html=True)

    st.markdown(f"""<div style='background:{card_bg}; border:1.5px solid {card_border}; border-radius:18px; padding:20px 24px; box-shadow:0 4px 16px rgba(0,0,0,{"0.25" if is_dark else "0.04"});'>
<div style='font-size:1.1rem; font-weight:700; color:{text_color}; display:flex; align-items:center; gap:8px;'>
<span>📄</span> รายงานสรุปสุขภาพและการกกสำหรับส่งสัตวแพทย์
</div>
<div style='font-size:0.85rem; color:{sub_color}; margin-top:4px;'>
จัดทำแบบฟอร์มมาตรฐานสรุปค่าเฉลี่ยอุณหภูมิ ความชื้น น้ำหนัก และการวิเคราะห์ AI พร้อมสำหรับสั่งพิมพ์หรือบันทึกเป็น PDF
</div>
</div>""", unsafe_allow_html=True)

    st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
    show_pdf_preview = st.checkbox("👁️ แสดงตัวอย่างเอกสารรายงานพร้อมพิมพ์ (Print / PDF Preview)", value=False)

    if show_pdf_preview:
        st.markdown(f"""<div style='background:#ffffff; color:#0f172a; padding:25px; border-radius:14px; border:2px solid #e2e8f0; max-width:700px; margin:12px auto; box-shadow:0 8px 24px rgba(0,0,0,0.15);'>
<div style='text-align:center; border-bottom:2px solid #f97316; padding-bottom:12px; margin-bottom:16px;'>
<div style='font-size:1.3rem; font-weight:700; color:#f97316;'>รายงานสรุปสภาวะการบริบาลลูกป้อน</div>
<div style='font-size:0.9rem; color:#64748b;'>{user_pref.get('farm_name', 'AI Smart Brooder')}</div>
<div style='font-size:0.8rem; color:#94a3b8;'>วันที่ออกเอกสาร: {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}</div>
</div>
<table style='width:100%; border-collapse:collapse; font-size:0.9rem;'>
<tr style='border-bottom:1px solid #cbd5e1;'><td style='padding:8px;'><b>อุณหภูมิเฉลี่ย</b></td><td style='padding:8px;'>{cur_t:.1f} °C (เป้าหมาย: {t_min:.1f} - {t_max:.1f} °C)</td></tr>
<tr style='border-bottom:1px solid #cbd5e1;'><td style='padding:8px;'><b>ความชื้นสัมพัทธ์</b></td><td style='padding:8px;'>{cur_h:.0f} %RH (เป้าหมาย: {h_min} - {h_max} %RH)</td></tr>
<tr style='border-bottom:1px solid #cbd5e1;'><td style='padding:8px;'><b>ระดับแอมโมเนีย</b></td><td style='padding:8px;'>{cur_nh3} ppm (เกณฑ์ปลอดภัย &lt; 10 ppm)</td></tr>
<tr style='border-bottom:1px solid #cbd5e1;'><td style='padding:8px;'><b>น้ำหนักรวมลูกนก</b></td><td style='padding:8px;'>{total_weight:.1f} กรัม (หักน้ำหนักรังแล้ว)</td></tr>
<tr style='border-bottom:1px solid #cbd5e1;'><td style='padding:8px;'><b>การวิเคราะห์ AI</b></td><td style='padding:8px;'>นอนหลับสบาย กระจายตัวสม่ำเสมอ อัตราการรอดชีวิต 100%</td></tr>
</table>
<div style='text-align:center; margin-top:20px;'>
<button onclick="window.print()" style="background:#f97316; color:white; border:none; padding:10px 26px; border-radius:10px; font-weight:600; font-family:'Prompt', sans-serif; cursor:pointer; box-shadow:0 4px 14px rgba(249,115,22,0.35);">
🖨️ สั่งพิมพ์เอกสาร / บันทึกเป็น PDF
</button>
</div>
</div>""", unsafe_allow_html=True)
