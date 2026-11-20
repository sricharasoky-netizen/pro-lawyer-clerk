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

# ขยายเวลา Timeout เป็น 22 วินาที เพื่อให้สมองหลักทำงานเสร็จโดยไม่ถูกตัดสายเร็วเกินไป
ai_client = genai.Client(
    api_key=GOOGLE_API_KEY,
    http_options=types.HttpOptions(timeout=22000),
)

PRIMARY_MODEL = "gemini-3.8-flash"

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
cached_models: list[str] = []

app = Flask(__name__)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def get_candidate_models() -> list[str]:
    """ดึงรายชื่อโมเดลที่มีอยู่จริงในบัญชี API Key อัตโนมัติ ป้องกัน Error 404"""
    global cached_models
    if cached_models:
        return cached_models

    models = [PRIMARY_MODEL]
    try:
        for m in ai_client.models.list():
            name = (getattr(m, "name", "") or "").replace("models/", "")
            if (
                "gemini" in name
                and "flash" in name
                and not any(skip in name for skip in ("tts", "image", "audio", "embedding", "vision"))
                and name not in models
            ):
                models.append(name)
    except Exception as e:
        print(f"⚠️ Cannot list models: {e}")

    for fallback in ("gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.0-flash"):
        if fallback not in models:
            models.append(fallback)

    cached_models = models[:4]
    return cached_models


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
    candidate_models = get_candidate_models()
    last_error = ""

    for idx, model_name in enumerate(candidate_models):
        cfg = SEARCH_CONFIG if (idx == 0 and use_search) else BASE_CONFIG
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=contents,
                config=cfg,
            )
            reply = (response.text or "").strip()
            if reply:
                user_histories[session_id] = (
                    contents + [types.Content(role="model", parts=[types.Part.from_text(text=reply)])]
                )[-8:]
                return reply
        except Exception as err:
            last_error = str(err)
            print(f"⚠️ Model {model_name} failed: {last_error}")
            # หากติดปัญหาจากโควตา Google Search ให้ลองรันโมเดลเดิมแบบไม่เปิด Search ทันที
            if cfg is SEARCH_CONFIG:
                try:
                    response = ai_client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=BASE_CONFIG,
                    )
                    reply = (response.text or "").strip()
                    if reply:
                        user_histories[session_id] = (
                            contents + [types.Content(role="model", parts=[types.Part.from_text(text=reply)])]
                        )[-8:]
                        return reply
                except Exception as retry_err:
                    last_error = str(retry_err)

    return f"⏳ ขณะนี้เซิร์ฟเวอร์ AI ต้นทางคิวแน่นชั่วคราว รบกวนเรียกน้อนหนูใหม่อีกครั้งนะคะ ({last_error[:100]})"


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
    return jsonify({"status": "ok", "models": get_candidate_models(), "group_shield": True})


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
