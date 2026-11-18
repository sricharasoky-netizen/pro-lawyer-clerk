import os
import time
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

# 1. โหลดกุญแจสำคัญ
load_dotenv(override=True)
env_vars = dotenv_values(".env")


def get_secret(key_name: str) -> str:
    val = env_vars.get(key_name) or os.getenv(key_name) or ""
    return val.strip().strip('"').strip("'")


GOOGLE_API_KEY = get_secret("GOOGLE_API_KEY") or get_secret("GEMINI_API_KEY")
LINE_CHANNEL_SECRET = get_secret("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = get_secret("LINE_CHANNEL_ACCESS_TOKEN")

if not GOOGLE_API_KEY:
    raise RuntimeError("❌ ไม่พบ GOOGLE_API_KEY ในระบบ")
if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("❌ ไม่พบค่าเชื่อมต่อ LINE ในระบบ")

# 2. ตั้งค่าสมอง Gemini + ระบบสำรองป้องกัน 429 Quota
ai_client = genai.Client(api_key=GOOGLE_API_KEY)
PRIMARY_MODEL = "gemini-3.8-flash"
FALLBACK_MODELS = ["gemini-3.8-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash"]

# โหมดปกติ (ประหยัดโควตา ตอบไว)
BASE_CONFIG = types.GenerateContentConfig(
    system_instruction=MASTER_LEGAL_INSTRUCTION,
    temperature=0.2,
)

# โหมดเรดาร์ค้นฎีกาจริงผ่าน Google Search (เปิดใช้เฉพาะเมื่อสั่งค้นหา)
SEARCH_CONFIG = types.GenerateContentConfig(
    system_instruction=MASTER_LEGAL_INSTRUCTION,
    temperature=0.2,
    tools=[types.Tool(google_search=types.GoogleSearch())],
)

SEARCH_KEYWORDS = ("ฎีกา", "ค้น", "สืบค้น", "หาข้อมูล", "ล่าสุด", "เว็บ", "อ้างอิง", "พ.ร.บ.", "พระราชบัญญัติ")

# เก็บประวัติการคุยรายบุคคล (สูงสุด 10 รอบล่าสุด เพื่อไม่ให้เปลืองโควตา Token สะสม)
user_histories: dict[str, list[types.Content]] = {}

app = Flask(__name__)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def needs_google_search(text: str) -> bool:
    return any(kw in text for kw in SEARCH_KEYWORDS)


def generate_smart_reply(user_id: str, user_text: str) -> str:
    history = user_histories.get(user_id, [])
    contents = history + [types.Content(role="user", parts=[types.Part.from_text(text=user_text)])]

    use_search = needs_google_search(user_text)
    configs_to_try = [SEARCH_CONFIG, BASE_CONFIG] if use_search else [BASE_CONFIG]
    models_to_try = [PRIMARY_MODEL] + FALLBACK_MODELS

    last_err = None
    for model_name in models_to_try:
        for cfg in configs_to_try:
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=cfg,
                )
                reply = (response.text or "").strip()
                if reply:
                    # บันทึกความจำเฉพาะ 10 ข้อความล่าสุดเพื่อประหยัดโควตา
                    new_history = contents + [
                        types.Content(role="model", parts=[types.Part.from_text(text=reply)])
                    ]
                    user_histories[user_id] = new_history[-10:]
                    return reply
            except Exception as err:
                last_err = err
                err_str = str(err)
                print(f"⚠️ [{model_name} | search={cfg == SEARCH_CONFIG}] Error: {err_str}")
                # ถ้าโมเดลไม่มีอยู่จริง ข้ามไปลองโมเดลถัดไปทันที
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break
                # ถ้าติด 429 หากเปิด Search อยู่ ให้สลับลองแบบไม่เปิด Search หรือพัก 2 วินาทีก่อนลองรุ่นสำรอง
                time.sleep(1.5)

    if last_err and "429" in str(last_err):
        return "⏳ ขณะนี้โควตาการประมวลผลรายนาทีของ Google เต็มชั่วคราวค่ะคุณครู รบกวนรอประมาณ ๓๐-๖๐ วินาทีแล้วส่งคำสั่งใหม่อีกครั้งนะคะ"
    return "ขออภัยค่ะคุณครู ระบบประมวลผลขัดข้องชั่วคราว กรุณาส่งคำสั่งใหม่อีกครั้งนะคะ"


def split_text_for_line(text: str, max_length: int = 4500) -> list[str]:
    text = text.strip()
    if not text:
        return ["ขออภัยค่ะ ระบบไม่สามารถสร้างคำตอบได้ในขณะนี้ค่ะ"]
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
            "bot": "Pro Lawyer Clerk 8-in-1 (Smart Anti-429)",
            "model": PRIMARY_MODEL,
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
    user_id = event.source.user_id or "default_user"
    user_text = event.message.text.strip()

    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)

        if user_text.lower() in {"เริ่มใหม่", "ล้างความจำ", "reset", "clear"}:
            user_histories.pop(user_id, None)
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

        if event.source.user_id:
            try:
                line_bot_api.show_loading_animation(
                    ShowLoadingAnimationRequest(chatId=event.source.user_id, loadingSeconds=40)
                )
            except Exception:
                pass

        reply_text = generate_smart_reply(user_id, user_text)
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
