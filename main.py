import os
from google import genai
import streamlit as st
from dotenv import load_dotenv

# โหลดค่า Environment Variables (รองรับทั้งไฟล์ .env และ Streamlit Secrets)
load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or st.secrets.get("GOOGLE_API_KEY", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or st.secrets.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET") or st.secrets.get("LINE_CHANNEL_SECRET", "")

# ตั้งค่าหน้าเว็บ Streamlit Dashboard
st.set_page_config(page_title="Pro Lawyer Clerk - Line Bot Status", page_icon="⚖️")

st.title("⚖️ Pro Lawyer Clerk - LINE Bot System")
st.markdown("ระบบหลังบ้านและสถานะการทำงานของบอทกฎหมายบน Streamlit Cloud")

# ตรวจสอบสถานะการเชื่อมต่อ Secrets
with col1 := st.container():
    if GOOGLE_API_KEY and LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
        st.success("สถานะระบบ: พร้อมเชื่อมต่อ (Connected)")
    else:
        st.error("สถานะระบบ: ขาดข้อมูล Secrets กรุณาตรวจสอบการตั้งค่า")

with col2 := st.container():
    st.metric(label="โหมดการทำงาน", value="Dashboard Active")

st.markdown("---")
st.subheader("🛠️ คำแนะนำการใช้งาน")
st.markdown("""
1. ตรวจสอบสถานะสีเขียวด้านบนว่าพร้อมเชื่อมต่อหรือไม่
2. หากตั้งค่า Secrets บนคลาวด์ครบถ้วน บอทใน LINE จะสามารถรับส่งข้อความและรูปภาพได้ทันที
""")

# ฟังก์ชันทดสอบการเรียกใช้งาน Gemini AI (อัปเดตใช้ SDK และ Model ล่าสุด)
def ask_gemini(prompt_text):
    if not GOOGLE_API_KEY:
        return "กรุณาตั้งค่า GOOGLE_API_KEY ก่อนใช้งาน"
    try:
        client = genai.Client(api_key=GOOGLE_API_KEY)
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt_text
        )
        return response.text
    except Exception as e:
        return f"เกิดข้อผิดพลาด: {str(e)}"
