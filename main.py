import os
import threading
from flask import Flask, request, abort
from dotenv import load_dotenv
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

# --- ตั้งค่าเริ่มต้น ---
load_dotenv()
app = Flask(__name__)
genai.configure(api_key=os.getenv('GOOGLE_API_KEY'))

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# --- MASTER SYSTEM INSTRUCTION ---
MASTER_LEGAL_INSTRUCTION = """คุณคือ "ระบบปฏิบัติการกฎหมายอัจฉริยะขั้นสูง (Legal Lifecycle OS)" ที่รวมจิตวิญญาณและความเชี่ยวชาญของ ๔ ยอดนักกฎหมายไว้ในหนึ่งเดียว 

เมื่อคุณได้รับข้อเท็จจริงของคดี (Case Fact) หรือรหัสคำสั่ง "พลังสื่อ" ให้คุณสวมบทบาทประมวลผลและแสดงผลลัพธ์แยกออกเป็น ๔ ส่วนตามลำดับ (Step-by-step) อย่างเคร่งครัด ดังนี้:

[ส่วนที่ ๑: ตรวจสำนวนและพิทักษ์สิทธิ]
- สแกนหาช่องโหว่ ความไม่ชอบมาพากล และการละเมิดสิทธิในสำนวนคดี (เน้นคดีอาญา/แพ่งที่เข้าข่ายฟ้องกลั่นแกล้ง SLAPP)
- ทำ Gap Analysis ตรวจสอบความสอดคล้องของพยานหลักฐานและชี้จุดที่ข้อมูลขาดหาย
- ตัดเนื้อความน้ำท่วมทุ่งออก เน้น Fact-Based สกัดใจความสำคัญเป็นหัวข้อสั้นกระชับ

[ส่วนที่ ๒: ที่ปรึกษากฎหมายเชิงยุทธศาสตร์]
- ให้เปลี่ยนโหมดมาแทนตนเองว่า "ผม" หรือ "ดิฉัน" และเรียกผู้ใช้ว่า "ท่าน" เสมอในส่วนนี้
- วิเคราะห์พยานหลักฐานดิจิทัล (Forensic Evidence) หาจุดย้อนแย้งและช่องว่างของเวลา (Timeline)
- ทำ Psychological Profiling วิเคราะห์มูลเหตุจูงใจ (Motive) เพื่อวางแผนซักค้าน และใช้ Reverse Logic คิดมุมกลับเพื่อดักทางคู่กรณี

[ส่วนที่ ๓: เสมียนทนายอาวุโส]
- สรุปแนวคำพิพากษาศาลฎีกาที่เกี่ยวข้องตามหลัก "10-Deka Retrieval Protocol" (เรียงปีล่าสุด พ.ศ. ๒๕๖๙ ถอยหลัง)
- แยกหัวข้อ "ส่วนสารบัญญัติ" และ "ส่วนวิธีสบัญญัติ" ให้ชัดเจน ระบุแหล่งที่มาและมาตรากฎหมายให้ถูกต้อง ห้ามมโนเลขมาตรา

[ส่วนที่ ๔: การบริหารจัดการคดีและสรุปภาพรวม (Legal Lifecycle OS)]
- สรุปแนวทางปฏิบัติของคดีตั้งแต่ชั้นเตรียมฟ้องจนถึงบังคับคดี รักษาบริบทข้อเท็จจริงเดิมไว้
- หากผู้ใช้ระบุรหัสคำสั่งระบบย่อย "พลังสื่อ ๐๑ ถึง พลังสื่อ ๓๗" (เช่น พลังสื่อ ๑๔) ให้ทำหน้าที่ร่างและประมวลผลเอกสารตามหมวดคำสั่งนั้นในส่วนนี้ทันที

⚠️ กฎเหล็กขั้นเด็ดขาด: ต้องใช้ "ตัวเลขไทย" (๑, ๒, ๓, ๔, ๕...) ในการรายงานผล ร่างเอกสาร และการระบุมาตรา/ปี พ.ศ. ทั้งหมดในทุกส่วนอย่างเคร่งครัด!"""

def call_master_legal_gemini(case_fact):
    model = genai.GenerativeModel(
        model_name='gemini-2.5-flash',
        system_instruction=MASTER_LEGAL_INSTRUCTION
    )
    prompt = f"ข้อเท็จจริงของคดี: {case_fact}\n\nจงวิเคราะห์คดีนี้ตามลำดับทั้ง ๔ ส่วนอย่างละเอียด"
    response = model.generate_content(prompt)
    return response.text

def send_long_message(user_id, text):
    max_length = 1900
    if len(text) <= max_length:
        line_bot_api.push_message(user_id, TextSendMessage(text=text))
    else:
        paragraphs = text.split('\n')
        current_chunk = ""
        for paragraph in paragraphs:
            if len(current_chunk) + len(paragraph) + 1 > max_length:
                line_bot_api.push_message(user_id, TextSendMessage(text=current_chunk.strip()))
                current_chunk = paragraph + "\n"
            else:
                current_chunk += paragraph + "\n"
        if current_chunk:
            line_bot_api.push_message(user_id, TextSendMessage(text=current_chunk.strip()))

def run_legal_pipeline_optimized(user_id, case_fact):
    try:
        final_report = call_master_legal_gemini(case_fact)
        send_long_message(user_id, final_report)
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg:
            line_bot_api.push_message(user_id, TextSendMessage(text="⚠️ [Resource Exhausted 429] โควต้าเต็ม กรุณารอ ๑ นาทีแล้วลองใหม่อีกครั้งค่ะ"))
        else:
            line_bot_api.push_message(user_id, TextSendMessage(text=f"ระบบพบข้อผิดพลาด: {error_msg}"))

@app.route("/webhook", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text
    if "ตรวจคดี" in msg or "พลังสื่อ" in msg:
        case_fact = msg.replace("ตรวจคดี", "").replace("พลังสื่อ", "").strip() or msg
        line_bot_api.reply_message(
            event.reply_token, 
            TextSendMessage(text="🤖 ระบบได้รับข้อเท็จจริงแล้ว กำลังวิเคราะห์ ๔ ส่วนแบบม้วนเดียวจบ (๕-๗ วินาที)")
        )
        threading.Thread(target=run_legal_pipeline_optimized, args=(event.source.user_id, case_fact)).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
