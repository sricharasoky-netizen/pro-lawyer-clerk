import os
from dotenv import dotenv_values, load_dotenv
from flask import Flask, abort, jsonify, request
from google import genai
from google.genai import types
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from instructions import MASTER_LEGAL_INSTRUCTION

# 1. โหลดกุญแจสำคัญ (รองรับทั้งไฟล์ .env ในเครื่อง และ Environment บน Cloud)
load_dotenv(override=True)
env_vars = dotenv_values(".env")


def get_secret(key_name: str) -> str:
    val = env_vars.get(key_name) or os.getenv(key_name) or ""
    return val.strip().strip('"').strip("'")


GOOGLE_API_KEY = get_secret("GOOGLE_API_KEY") or get_secret("GEMINI_API_KEY")
LINE_CHANNEL_SECRET = get_secret("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = get_secret("LINE_CHANNEL_ACCESS_TOKEN")

if not GOOGLE_API_KEY:
    raise RuntimeError("❌ ไม่พบ GOOGLE_API_KEY ในระบบ กรุณาตรวจสอบไฟล์ .env")
if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("❌ ไม่พบค่าเชื่อมต่อ LINE ในไฟล์ .env")

# 2. ตั้งค่าสมอง Gemini 3.8 Flash + Google Search ค้นฎีกาจริง
ai_client = genai.Client(api_key=GOOGLE_API_KEY)
MODEL_NAME = "gemini-3.8-flash"

AI_CONFIG = types.GenerateContentConfig(
    system_instruction=MASTER_LEGAL_INSTRUCTION,
    temperature=0.2,
    tools=[types.Tool(google_search=types.GoogleSearch())],
)

# เก็บความจำแยกตามรายบุคคล เพื่อให้กดสลับโหมด 1-8 และคุยต่อเนื่องได้
user_chats = {}

# 3. ตั้งค่า Flask App และ LINE Messaging API (SDK v3)
app = Flask(__name__)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def split_text_for_line(text: str, max_length: int = 4500) -> list[str]:
    """หั่นข้อความยาวเกินลิมิต LINE ออกเป็นส่วนๆ อัตโนมัติ (สูงสุด 5 บอลลูน)"""
    text = text.strip()
    if not text:
        return ["ขออภัยค่ะ ระบบไม่สามารถสร้างคำตอบได้ในขณะนี้ กรุณาลองใหม่อีกครั้งนะคะ"]
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text and len(chunks) < 5:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_pos = text.rfind("\n", 0, max_length)
        if split_pos == -1 or split_pos < max_length // 2:
            split_pos = max_length
        chunks.append(text[:split_pos].strip())
        text = text[split_pos:].strip()
    return chunks


@app.route("/", methods=["GET"])
def health_check():
    return jsonify(
        {
            "status": "ok",
            "bot": "Pro Lawyer Clerk 8-in-1 (Flask)",
            "model": MODEL_NAME,
            "google_search_grounding": True,
        }
    )


@app.route("/callback", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, description="Invalid LINE signature")

    return "OK", 200


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    user_id = event.source.user_id
    user_text = event.message.text.strip()

    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)

        # คำสั่งล้างความจำเพื่อเริ่มคดีใหม่
        if user_text.lower() in {"เริ่มใหม่", "ล้างความจำ", "reset", "clear"}:
            user_chats.pop(user_id, None)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(
                            text="🧹 ล้างความจำบทสนทนาเดิมเรียบร้อยแล้วค่ะคุณครู พร้อมรับคำสั่งคดีใหม่หรือกดเลือกโหมด ๑-๘ ได้เลยค่ะ!"
                        )
                    ],
                )
            )
            return

        # แสดงจุดกระพริบกำลังโหลดระหว่างวิ่งค้นฎีกาใน Google
        if user_id:
            try:
                line_bot_api.show_loading_animation(
                    ShowLoadingAnimationRequest(chatId=user_id, loadingSeconds=40)
                )
            except Exception:
                pass

        if user_id not in user_chats:
            user_chats[user_id] = ai_client.chats.create(
                model=MODEL_NAME,
                config=AI_CONFIG,
            )

        try:
            response = user_chats[user_id].send_message(user_text)
            reply_text = response.text or "ไม่พบข้อมูลตอบกลับจากระบบค่ะ"
        except Exception:
            # กรณีเซสชันสะดุด ให้สร้างห้องความจำใหม่แล้วยิงซ้ำอัตโนมัติ 1 ครั้ง
            try:
                user_chats[user_id] = ai_client.chats.create(
                    model=MODEL_NAME,
                    config=AI_CONFIG,
                )
                response = user_chats[user_id].send_message(user_text)
                reply_text = response.text or "ไม่พบข้อมูลตอบกลับจากระบบค่ะ"
            except Exception as retry_err:
                print(f"⚠️ Gemini Error: {retry_err}")
                reply_text = "ขออภัยค่ะคุณครู ระบบประมวลผลขัดข้องชั่วคราว กรุณาส่งคำสั่งใหม่อีกครั้งนะคะ"

        chunks = split_text_for_line(reply_text)
        messages = [TextMessage(text=chunk) for chunk in chunks]

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=messages,
            )
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 เปิดเซิร์ฟเวอร์ Flask เสมียนทนายมือโปร 8-in-1 ที่พอร์ต {port}...")
    app.run(host="0.0.0.0", port=port, debug=False)

