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

load_dotenv(override=True)
env_vars = dotenv_values(".env")


def get_secret(key_name: str) -> str:
    val = env_vars.get(key_name) or os.getenv(key_name) or ""
    return val.strip().strip('"').strip("'")


GOOGLE_API_KEY = get_secret("GOOGLE_API_KEY") or get_secret("GEMINI_API_KEY")
LINE_CHANNEL_SECRET = get_secret("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = get_secret("LINE_CHANNEL_ACCESS_TOKEN")

ai_client = genai.Client(
    api_key=GOOGLE_API_KEY,
    http_options=types.HttpOptions(timeout=22000),  # ล็อกเวลาไม่เกิน 22 วินาที กัน Gunicorn ตัดไฟ!
)
MODEL_NAME = "gemini-3.8-flash"

BASE_CONFIG = types.GenerateContentConfig(
    system_instruction=MASTER_LEGAL_INSTRUCTION,
    temperature=0.2,
)

SEARCH_CONFIG = types.GenerateContentConfig(
    system_instruction=MASTER_LEGAL_INSTRUCTION,
    temperature=0.2,
    tools=[types.Tool(google_search=types.GoogleSearch())],
)

SEARCH_KEYWORDS = ("ฎีกา", "ค้น", "สืบค้น", "หาข้อมูล", "ล่าสุด", "เว็บ", "อ้างอิง", "พ.ร.บ.", "พระราชบัญญัติ")
user_histories: dict[str, list[types.Content]] = {}

app = Flask(__name__)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def generate_fast_reply(user_id: str, user_text: str) -> str:
    history = user_histories.get(user_id, [])
    contents = history + [types.Content(role="user", parts=[types.Part.from_text(text=user_text)])]
    use_search = any(kw in user_text for kw in SEARCH_KEYWORDS)
    cfg = SEARCH_CONFIG if use_search else BASE_CONFIG

    try:
        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=cfg,
        )
        reply = (response.text or "").strip()
        if reply:
            user_histories[user_id] = (
                contents + [types.Content(role="model", parts=[types.Part.from_text(text=reply)])]
            )[-8:]
            return reply
        return "ไม่พบข้อความตอบกลับจากระบบค่ะคุณครู"
    except Exception as err:
        err_str = str(err)
        print(f"⚠️ Gemini Error: {err_str}")
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            return "⏳ โควตาของ API Key ปัจจุบันเต็ม (429) ค่ะคุณครู แนะนำให้สร้าง API Key ใหม่แบบ 'Create API key in new project' ใน Google AI Studio (จะได้รหัสขึ้นต้นด้วย AIzaSy...) แล้วนำมาใส่ใน Render Environment นะคะ"
        return f"⚠️ ระบบแจ้งเตือนขัดข้องชั่วคราวค่ะคุณครู ({err_str[:120]})"


def split_text_for_line(text: str, max_length: int = 4500) -> list[str]:
    text = text.strip() or "ขออภัยค่ะ ระบบไม่สามารถสร้างคำตอบได้ในขณะนี้ค่ะ"
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
    return jsonify({"status": "ok", "model": MODEL_NAME})


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
                    messages=[TextMessage(text="🧹 ล้างความจำเรียบร้อยแล้วค่ะคุณครู พร้อมรับคำสั่งใหม่ค่ะ!")],
                )
            )
            return

        if event.source.user_id:
            try:
                line_bot_api.show_loading_animation(
                    ShowLoadingAnimationRequest(chatId=event.source.user_id, loadingSeconds=25)
                )
            except Exception:
                pass

        reply_text = generate_fast_reply(user_id, user_text)
        messages = [TextMessage(text=c) for c in split_text_for_line(reply_text)]
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=messages))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
