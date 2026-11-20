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
                    i
