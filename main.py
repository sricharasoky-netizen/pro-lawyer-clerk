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

# ตั้ง Timeout กระชับเพื่อสลับโมเดลสำรองได้ทันภายใน 25 วินาที (ก่อน Gunicorn ตัดที่ 30 วินาที)
ai_client_primary = genai.Client(
    api_key=GOOGLE_API_KEY,
    http_options=types.HttpOptions(timeout=12000),
)
ai_client_backup = genai.Client(
    api_key=GOOGLE_API_KEY,
    http_options=types.HttpOptions(timeout=10000),
)

PRIMARY_MODEL = "gemini-2.5-flash"
BACKUP_MODEL = "gemini-2.5-flash-lite"  # สมองสำรองความเร็วสูง แก้ปัญหา 503 High Demand ช่วงหัวค่ำ

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
WAKE_WORDS = ("น้อนหนู", "@น้อนหนู", "เสมียน", "@เสมียน")

user_histories: dict[str, list[types.Content]] = {}

app = Flask(__name__)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def get_chat_session_id(event: MessageEvent) -> str:
    source_type = getattr(event.source, "type", "user")
    if source_type == "group":
        return f"group_{getattr(event.source, 'group_id', 'default')}"
    if source_type == "room":
        return f"room_{getattr(event.source, 'room_id', 'default')}"
    return f"user_{getattr(event.source, 'user_id', 'default')}"


def generate_smart_reply(session_id: str, user_text: str, is_group: bool) -> str | None:
    history = user_histories.get(session_id, [])
    contents = history + [types.Content(role="user", parts=[types.Part.from_text(text=user_text)])]
    use_search = any(kw in user_text for kw in SEARCH_KEYWORDS)
    primary_cfg = SEARCH_CONFIG if use_search else BASE_CONFIG

    # ชั้นที่ 1: ลองสมองหลัก (gemini-3.8-flash)
    try:
        response = ai_client_primary.models.generate_content(
            model=PRIMARY_MODEL,
            contents=contents,
            config=primary_cfg,
        )
        reply = (response.text or "").strip()
        if reply:
            user_histories[session_id] = (
                contents + [types.Content(role="model", parts=[types.Part.from_text(text=reply)])]
            )[-8:]
            return reply
    except Exception as primary_err:
        print(f"⚠️ Primary model busy ({primary_err}), switching to backup...")

    # ชั้นที่ 2: สลับสมองสำรองความเร็วสูง (gemini-3.8-flash-lite) ทันทีเมื่อติด 503 / 504 / 429
    try:
        response = ai_client_backup.models.generate_content(
            model=BACKUP_MODEL,
            contents=contents,
            config=BASE_CONFIG,
        )
        reply = (response.text or "").strip()
        if reply:
            user_histories[session_id] = (
                contents + [types.Content(role="model", parts=[types.Part.from_text(text=reply)])]
            )[-8:]
            return reply
    except Exception as backup_err:
        err_str = str(backup_err)
        print(f"⚠️ Backup model error: {err_str}")
        if is_group:
            return "⏳ ขณะนี้เซิร์ฟเวอร์ AI ต้นทางมีผู้ใช้งานหนาแน่นชั่วคราว รบกวนเรียกน้อนหนูใหม่อีกครั้งในอีกสักครู่นะคะ"
        return f"⏳ ขณะนี้เซิร์ฟเวอร์ AI ต้นทางคิวแน่นชั่วคราวค่ะคุณครู กรุณาลองใหม่อีกครั้งในอีกสักครู่นะคะ ({err_str[:80]})"

    return None


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
    return jsonify({"status": "ok", "primary": PRIMARY_MODEL, "backup": BACKUP_MODEL, "group_shield": True})


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
    source_type = getattr(event.source, "type", "user")
    is_group = source_type in ("group", "room")
    session_id = get_chat_session_id(event)
    raw_text = event.message.text.strip()

    # เกราะป้องกันไลน์กลุ่ม: ถ้าไม่ได้ขึ้นต้นด้วยคำว่า "น้อนหนู" หรือ "เสมียน" ให้เงียบสนิททันที 100%
    if is_group:
        matched_wake = next((w for w in WAKE_WORDS if raw_text.startswith(w)), None)
        if not matched_wake:
            return
        user_text = raw_text[len(matched_wake):].strip(" :,-")
        if not user_text:
            user_text = "สวัสดีจ้าน้อนหนู รายงานตัวสั้นๆ ให้พี่ๆ ทนายความในกลุ่มฟังหน่อย"
    else:
        user_text = raw_text

    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)

        if user_text.lower() in {"เริ่มใหม่", "ล้างความจำ", "reset", "clear"}:
            user_histories.pop(session_id, None)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="🧹 ล้างความจำห้องนี้เรียบร้อยแล้วค่ะ พร้อมรับคำสั่งใหม่ค่ะ!")],
                )
            )
            return

        if not is_group and getattr(event.source, "user_id", None):
            try:
                line_bot_api.show_loading_animation(
                    ShowLoadingAnimationRequest(chatId=event.source.user_id, loadingSeconds=25)
                )
            except Exception:
                pass

        reply_text = generate_smart_reply(session_id, user_text, is_group=is_group)
        if reply_text:
            messages = [TextMessage(text=c) for c in split_text_for_line(reply_text)]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=messages))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
