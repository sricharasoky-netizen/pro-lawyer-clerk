# requirements.txt ให้ใช้ตามนี้
# flask
# python-dotenv
# line-bot-sdk
# google-genai
# pyngrok

import os, time, threading
from flask import Flask, request, abort
from dotenv import load_dotenv
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, TextMessage as V3TextMessage, PushMessageRequest
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai
from google.genai import types

load_dotenv()
app = Flask(__name__)

# --- ตั้งค่า ---
client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

MASTER_LEGAL_INSTRUCTION = """ใส่ MASTER_LEGAL_INSTRUCTION เดิมของคุณพี่ทั้งหมดตรงนี้เหมือนเดิมได้เลยครับ"""

FALLBACK_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash-latest", "gemini-1.5-flash-8b-latest"]

def call_master_legal_gemini(case_fact):
    prompt = f"ข้อเท็จจริงของคดี: {case_fact}\n\nจงวิเคราะห์คดีนี้ตามลำดับทั้ง ๔ ส่วนอย่างละเอียด"
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
            print(f"❌ {model_name} พัง: {last_error}")
            if "404" in last_error or "429" in last_error or "quota" in last_error.lower():
                time.sleep(1)
                continue
            break
    raise Exception(last_error)

def send_long_message(user_id, text):
    max_len = 1800
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        if len(text) <= max_len:
            line_bot_api.push_message(PushMessageRequest(to=user_id, messages=[V3TextMessage(text=text)]))
        else:
            # ตัดส่งเป็นท่อน
            for i in range(0, len(text), max_len):
                chunk = text[i:i+max_len]
                line_bot_api.push_message(PushMessageRequest(to=user_id, messages=[V3TextMessage(text=chunk)]))
                time.sleep(0.6)

def run_pipeline(user_id, case_fact):
    try:
        final_report = call_master_legal_gemini(case_fact)
        send_long_message(user_id, final_report)
    except Exception as e:
        err = str(e)
        if "429" in err: msg = "⚠️ [429] โควต้าสายฟรีเต็ม กรุณารอ ๑ นาทีแล้วพิมพ์ \"ตรวจคดี\" ใหม่อีกครั้งครับ"
        else: msg = f"ระบบขัดข้อง: {err[:1000]}"
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=user_id, messages=[V3TextMessage(text=msg)]))

@app.route("/webhook", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    msg = event.message.text
    if "ตรวจคดี" in msg or "พลังสื่อ" in msg:
        case_fact = msg.replace("ตรวจคดี","").replace("พลังสื่อ","").strip() or msg
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                reply_token=event.reply_token,
                messages=[V3TextMessage(text="🤖 ได้รับข้อเท็จจริงแล้ว กำลังวิเคราะห์ ๔ ส่วนแบบม้วนเดียวจบ (๕-๗ วินาที)")]
            )
        threading.Thread(target=run_pipeline, args=(event.source.user_id, case_fact), daemon=True).start()

if __name__ == "__main__":
    # ถ้าจะรัน local ให้ใช้ ngrok ถ้าจะ deploy บน Render ไม่ต้องใช้ส่วนนี้
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))