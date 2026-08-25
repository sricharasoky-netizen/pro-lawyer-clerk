import os
import time
import threading
from flask import Flask, request, abort
from dotenv import load_dotenv

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, TextMessage, PushMessageRequest
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import google.genai as genai
from google.genai import types

load_dotenv()
app = Flask(__name__)

# ตั้งค่า Google GenAI Client และ LINE API
client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# คำสั่ง Master Legal Instruction
MASTER_LEGAL_INSTRUCTION = """คุณคือ "Pro Lawyer Clerk" ผู้ช่วยวิเคราะห์คดีความด้านกฎหมายไทย

เมื่อคุณได้รับข้อเท็จจริงของคดี (Case Fact) ให้วิเคราะห์และแสดงผลลัพธ์แยกเป็น 4 ส่วนตามลำดับ (Step-by-step) อย่างเคร่งครัด ดังนี้:

[ส่วนที่ 1: ตรวจสำนวนและพิทักษ์สิทธิ (Fact & Rights Check)]
- สแกนหาช่องโหว่ ความไม่ชอบมาพากล และความเสี่ยงต่อการถูกละเมิดสิทธิในสำนวนคดี
- ตรวจสอบความสอดคล้องของพยานหลักฐานและชี้จุดที่ข้อมูลยังขาดหาย
- สรุปเฉพาะข้อเท็จจริงที่จำเป็น เป็นหัวข้อสั้นกระชับ ไม่พรรณนาเกินความจำเป็น

[ส่วนที่ 2: วิเคราะห์เชิงกลยุทธ์ (Strategic Analysis)]
- วิเคราะห์พยานหลักฐานและลำดับเหตุการณ์ (Timeline) เพื่อหาจุดที่ต้องตรวจสอบเพิ่มเติม
- ประเมินแนวทางที่เป็นไปได้สำหรับการเตรียมคดี รวมถึงประเด็นที่ควรระวังจากฝ่ายตรงข้าม

[ส่วนที่ 3: ค้นคว้าแนวคำพิพากษา (Case Law Research)]
- สรุปแนวคำพิพากษาศาลฎีกาที่เกี่ยวข้อง เรียงจากปีล่าสุดย้อนหลัง
- แยกหัวข้อ "ส่วนสารบัญญัติ" และ "ส่วนวิธีสบัญญัติ" ให้ชัดเจน ระบุแหล่งที่มาและมาตรากฎหมายให้ถูกต้อง ห้ามระบุเลขมาตราที่ไม่มีแหล่งอ้างอิงจริง

[ส่วนที่ 4: สรุปภาพรวมและแนวทางดำเนินคดี (Case Overview & Roadmap)]
- สรุปแนวทางปฏิบัติของคดีตั้งแต่ชั้นเตรียมฟ้องจนถึงชั้นบังคับคดี โดยรักษาบริบทข้อเท็จจริงเดิมไว้ตลอด
- ระบุขั้นตอนถัดไปที่ผู้ใช้ควรดำเนินการ พร้อมข้อควรระวัง

หลักการเขียนผลลัพธ์: ใช้ภาษาที่เป็นทางการ กระชับ ชัดเจน ตรวจสอบย้อนกลับได้ และไม่คาดเดาข้อเท็จจริงที่ไม่ได้รับมา"""

# รุ่นโมเดล Gemini ที่ใช้งานได้จริง
FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite-preview"
]

def call_master_legal_gemini(case_fact):
    prompt = f"ข้อเท็จจริงของคดี: {case_fact}\n\nจงวิเคราะห์คดีนี้ตามลำดับทั้ง 4 ส่วนอย่างละเอียด"
    last_error = ""
    for model_name in FALLBACK_MODELS:
        try:
            print(f"⏳ ลองโมเดล: {model_name}")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=MASTER_LEGAL_INSTRUCTION,
                    temperature=0.2
                )
            )
            print(f"✅ สำเร็จด้วย: {model_name}")
            return response.text
        except Exception as e:
            last_error = str(e)
            print(f"❌ {model_name} พลาด: {last_error}")
            if "404" in last_error or "429" in last_error or "quota" in last_error.lower():
                time.sleep(2)
                continue
            break
    raise Exception(last_error)

def async_process_legal_case(target_id, case_fact):
    """ฟังก์ชันทำงานเบื้องหลัง วิเคราะห์คดีและส่งข้อความตอบกลับไปยัง ID ปลายทาง"""
    try:
        legal_result = call_master_legal_gemini(case_fact)
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=target_id,
                    messages=[TextMessage(text=legal_result)]
                )
            )
    except Exception as e:
        print(f"❌ Async Process Error: {e}")
        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=target_id,
                        messages=[TextMessage(text=f"ระบบขัดข้อง: {str(e)}")]
                    )
                )
        except Exception as push_err:
            print(f"❌ Cannot push error message: {push_err}")

@app.route("/webhook", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()

    if user_text.startswith("ตรวจคดี"):
        # หา ID ปลายทางเพื่อตอบกลับให้ถูกห้อง (กลุ่ม หรือ แชตส่วนตัว)
        source_type = event.source.type
        if source_type == 'group':
            target_id = event.source.group_id
        elif source_type == 'room':
            target_id = event.source.room_id
        else:
            target_id = event.source.user_id

        case_fact = user_text[7:].strip()
        if not case_fact:
            case_fact = "ไม่มีการระบุข้อเท็จจริงเพิ่มเติม กรุณาวิเคราะห์ตามข้อความทั่วไป"

        # ส่งข้อความแจ้งเตือนเบื้องต้นก่อน
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=target_id,
                    messages=[TextMessage(text="🤖 ได้รับข้อเท็จจริงแล้ว กำลังวิเคราะห์ 4 ส่วนแบบละเอียด (5-10 วินาที)")]
                )
            )

        # เริ่มทำงานเบื้องหลังส่งไปที่ target_id
        thread = threading.Thread(
            target=async_process_legal_case,
            args=(target_id, case_fact)
        )
        thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
