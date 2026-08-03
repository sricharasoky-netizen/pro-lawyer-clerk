import os
import streamlit as st
import google.generativeai as genai

# --- CONFIG & SECRETS ---
st.set_page_config(page_title="Pro Lawyer Clerk - Line Bot Status", page_icon="⚖️", layout="centered")

# ดึงค่าความลับ (รองรับทั้งแบบรัน Local และ Streamlit Cloud Secrets)
try:
    LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or st.secrets.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET") or st.secrets.get("LINE_CHANNEL_SECRET", "")
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or st.secrets.get("GOOGLE_API_KEY", "")
except Exception:
    LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# ตั้งค่าระบบ AI
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

# --- STREAMLIT UI (แดชบอร์ดตรวจสอบสถานะระบบ) ---
st.title("⚖️ Pro Lawyer Clerk - LINE Bot System")
st.markdown("ระบบหลังบ้านและสถานะการทำงานของบอทกฎหมายบน Streamlit Cloud")

st.info("💡 ระบบนี้ถูกออกแบบให้พร้อมเชื่อมต่อและแสดงสถานะการทำงานอัตโนมัติ")

col1, col2 = st.columns(2)
with col1:
    if GOOGLE_API_KEY and LINE_CHANNEL_ACCESS_TOKEN:
        st.success("สถานะระบบ: พร้อมเชื่อมต่อ (Connected)")
    else:
        st.error("สถานะระบบ: ขาดข้อมูล Secrets กรุณาตรวจสอบการตั้งค่าบนคลาวด์")

with col2:
    st.metric(label="โหมดการทำงาน", value="Dashboard Active")

st.markdown("---")
st.subheader("🛠️ คำแนะนำการใช้งาน")
st.markdown("""
1. ตรวจสอบสถานะสีเขียวด้านบนว่าพร้อมเชื่อมต่อหรือไม่
2. หากตั้งค่า Secrets บนคลาวด์ครบถ้วน บอทใน LINE จะสามารถรับส่งข้อความและรูปภาพได้ทันที
""")
