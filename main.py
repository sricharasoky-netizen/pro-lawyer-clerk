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
    MessagingApiBlob,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    FileMessageContent,
    ImageMessageContent,
    MessageEvent,
    TextMessageContent,
)
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
MAX_FILE_BYTES = 10 * 1024 * 1024  # จำกัดขนาดไฟล์ใน LINE ไม่เกิน 10 MB เพื่อให้ตอบทันใน 25 วินาที

user_histories: dict[str, list[types.Content]] = {}
pending_media: dict[str, dict] = {}
cached_models: list[str] = []

app = Flask(__name__)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def get_candidate_models() -> list[str]:
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


def generate_smart_reply(
    session_id: str,
    user_text: str,
    media_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> str | None:
    history = user_histories.get(session_id, [])

    user_parts = []
    if media_bytes and mime_type:
        user_parts.append(types.Part.from_bytes(data=media_bytes, mime_type=mime_type))
    user_parts.append(types.Part.from_text(text=user_text))

    contents = history + [types.Content(role="user", parts=user_parts)]
    use_search = any(kw in user_text for kw in SEARCH_KEYWORDS) and not media_bytes
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
                # เก็บประวัติเฉพาะข้อความเพื่อไม่ให้หน่วยความจำเต็มจากไฟล์ภาพขนาดใหญ่
                text_only_turn = types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
                model_turn = types.Content(role="model", parts=[types.Part.from_text(text=reply)])
                user_histories[session_id] = (history + [text_only_turn, model_turn])[-8:]
                return reply
        except Exception as err:
            last_error = str(err)
            print(f"⚠️ Model {model_name} failed: {last_error}")
            if cfg is SEARCH_CONFIG:
                try:
                    response = ai_client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=BASE_CONFIG,
                    )
                    reply = (response.text or "").strip()
                    if reply:
                        text_only_turn = types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
                        model_turn = types.Content(role="model", parts=[types.Part.from_text(text=reply)])
                        user_histories[session_id] = (history + [text_only_turn, model_turn])[-8:]
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
    return jsonify(
        {
            "status": "ok",
            "models": get_candidate_models(),
            "group_shield": True,
            "vision_pdf_enabled": True,
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
    source_type = getattr(event.source, "type", "user")
    is_group = source_type in ("group", "room")
    session_id = get_chat_session_id(event)
    raw_text = event.message.text.strip()

    if is_group:
        matched_wake = next((w for w in WAKE_WORDS if raw_text.startswith(w)), None)
        if not matched_wake:
            return
        user_text = raw_text[len(matched_wake) :].strip(" :,-")
        if not user_text:
            if session_id in pending_media:
                user_text = "ช่วยอ่านและวิเคราะห์เอกสารหรือรูปภาพล่าสุดที่ส่งเข้ามาในห้องนี้ตามหลักกฎหมายให้หน่อยค่ะ"
            else:
                user_text = "สวัสดีจ้าน้อนหนู รายงานตัวสั้นๆ ให้พี่ๆ ทนายความในกลุ่มฟังหน่อย"
    else:
        user_text = raw_text

    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)

        if user_text.lower() in {"เริ่มใหม่", "ล้างความจำ", "reset", "clear"}:
            user_histories.pop(session_id, None)
            pending_media.pop(session_id, None)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="🧹 ล้างความจำและไฟล์ค้างในห้องนี้เรียบร้อยแล้วค่ะ พร้อมรับคำสั่งใหม่ค่ะ!")],
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

        # ดึงรูปหรือไฟล์ PDF ล่าสุดที่ส่งค้างไว้ในห้อง (ถ้ามี) มาวิเคราะห์ร่วมกับคำสั่งทันที
        media_item = pending_media.pop(session_id, None)
        media_bytes = media_item["bytes"] if media_item else None
        mime_type = media_item["mime_type"] if media_item else None

        reply_text = generate_smart_reply(
            session_id,
            user_text,
            media_bytes=media_bytes,
            mime_type=mime_type,
        )
        if reply_text:
            messages = [TextMessage(text=c) for c in split_text_for_line(reply_text)]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=messages))


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event: MessageEvent):
    source_type = getattr(event.source, "type", "user")
    is_group = source_type in ("group", "room")
    session_id = get_chat_session_id(event)

    with ApiClient(line_config) as api_client:
        blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)

        try:
            img_bytes = bytes(blob_api.get_message_content(event.message.id))
        except Exception as e:
            print(f"⚠️ Failed to download image: {e}")
            return

        if len(img_bytes) > MAX_FILE_BYTES:
            if not is_group:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="⚠️ รูปภาพมีขนาดใหญ่เกิน ๑๐ MB รบกวนส่งผ่านน้อนหนูบนเว็บ AiPASS แทนนะคะ")],
                    )
                )
            return

        # ในไลน์กลุ่ม: เก็บรูปไว้ในความจำเงียบๆ รอให้ทนายพิมพ์ "น้อนหนู..." สั่งงาน
        if is_group:
            pending_media[session_id] = {"bytes": img_bytes, "mime_type": "image/jpeg"}
            return

        # ในแชทส่วนตัว: อ่านและวิเคราะห์รูปให้ทันที
        if getattr(event.source, "user_id", None):
            try:
                line_bot_api.show_loading_animation(
                    ShowLoadingAnimationRequest(chatId=event.source.user_id, loadingSeconds=25)
                )
            except Exception:
                pass

        prompt = "ช่วยอ่านข้อความในภาพนี้ทั้งหมดอย่างละเอียด สรุปข้อเท็จจริง วันเวลา จำนวนเงิน คู่กรณี และจุดสังเกตทางกฎหมายให้ครบถ้วนค่ะ"
        reply_text = generate_smart_reply(session_id, prompt, media_bytes=img_bytes, mime_type="image/jpeg")
        if reply_text:
            messages = [TextMessage(text=c) for c in split_text_for_line(reply_text)]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=messages))


@handler.add(MessageEvent, message=FileMessageContent)
def handle_file_message(event: MessageEvent):
    source_type = getattr(event.source, "type", "user")
    is_group = source_type in ("group", "room")
    session_id = get_chat_session_id(event)
    file_name = getattr(event.message, "file_name", "document.pdf") or "document.pdf"

    if not file_name.lower().endswith(".pdf"):
        if not is_group:
            with ApiClient(line_config) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="📄 ขณะนี้น้อนหนูใน LINE รองรับเฉพาะไฟล์นามสกุล .pdf และรูปภาพนะคะ")],
                    )
                )
        return

    with ApiClient(line_config) as api_client:
        blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)

        try:
            pdf_bytes = bytes(blob_api.get_message_content(event.message.id))
        except Exception as e:
            print(f"⚠️ Failed to download PDF: {e}")
            return

        if len(pdf_bytes) > MAX_FILE_BYTES:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[
                        TextMessage(
                            text=f"📄 ไฟล์ {file_name} มีขนาดใหญ่เกิน ๑๐ MB แนะนำให้อัปโหลดให้น้อนหนูบนเว็บ AiPASS เพื่อวิเคราะห์สำนวนขนาดใหญ่ได้ครบทุกหน้าค่ะ"
                        )
                    ],
                )
            )
            return

        # ในไลน์กลุ่ม: เก็บไฟล์ PDF ไว้รอคำสั่ง "น้อนหนู..."
        if is_group:
            pending_media[session_id] = {"bytes": pdf_bytes, "mime_type": "application/pdf"}
            return

        # ในแชทส่วนตัว: อ่านและสรุปไฟล์ PDF ทันที
        if getattr(event.source, "user_id", None):
            try:
                line_bot_api.show_loading_animation(
                    ShowLoadingAnimationRequest(chatId=event.source.user_id, loadingSeconds=25)
                )
            except Exception:
                pass

        prompt = f"ช่วยอ่านไฟล์เอกสาร PDF ชื่อ '{file_name}' นี้และสรุปข้อเท็จจริง ประเด็นข้อกฎหมาย และจุดสำคัญให้ครบถ้วนค่ะ"
        reply_text = generate_smart_reply(session_id, prompt, media_bytes=pdf_bytes, mime_type="application/pdf")
        if reply_text:
            messages = [TextMessage(text=c) for c in split_text_for_line(reply_text)]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=messages))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)

