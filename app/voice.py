"""Дополнительные голосовые функции через прокси организаторов."""

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request as ProxyRequest, build_opener
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.llm import wait_for_sain_slot


router = APIRouter(prefix="/api/voice")
ORGANIZER_PROXY = "https://bifrost.be-healthy.tech/v1"
MAX_AUDIO_BYTES = 3 * 1024 * 1024
AUDIO_FORMATS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
}
MODEL_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Никогда не пересылаем запись и авторизацию другому адресу.
        return None


_opener = build_opener(_NoRedirect())


class ProxyError(Exception):
    pass


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


def _proxy_key():
    if os.getenv("HACK_VOICE_ENABLED") != "1" or os.getenv("LLM_PROVIDER", "").lower() != "sain":
        return None
    base_url = os.getenv("LLM_BASE_URL") or ORGANIZER_PROXY
    if base_url.rstrip("/") != ORGANIZER_PROXY:
        return None
    key = os.getenv("LLM_API_KEY", "").strip()
    return key if key.isascii() and key.isprintable() and not any(char.isspace() for char in key) else None


def _model(variable, default):
    model = os.getenv(variable) or default
    if not MODEL_NAME.fullmatch(model):
        raise HTTPException(503, "Речевая модель не настроена")
    return model


def voice_enabled():
    return bool(_proxy_key())


def _require_voice():
    key = _proxy_key()
    if not key:
        raise HTTPException(503, "Голосовая функция недоступна. Напишите просьбу в чате.")
    return key


async def _bounded_body(request, limit):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
            if length < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Некорректный размер запроса") from None
        if length > limit:
            raise HTTPException(413, "Запись или сообщение слишком длинные")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "Запись или сообщение слишком длинные")
        body.extend(chunk)
    if not body:
        raise HTTPException(400, "Запрос пуст")
    return body


def _content_type(request):
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _post_to_proxy(path, key, payload, content_type, response_types, limit):
    request = ProxyRequest(
        ORGANIZER_PROXY + path,
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
        method="POST",
    )
    wait_for_sain_slot()
    try:
        with _opener.open(request, timeout=20) as response:
            if response.headers.get_content_type() not in response_types:
                raise ProxyError
            response_body = response.read(limit + 1)
    except HTTPError as exc:
        exc.close()
        raise ProxyError from None
    except (URLError, OSError):
        raise ProxyError from None
    if not response_body or len(response_body) > limit:
        raise ProxyError
    return response_body


def _transcribe(key, model, audio, media_type):
    boundary = "sain-voice-" + uuid4().hex
    suffix = AUDIO_FORMATS[media_type]
    fields = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"request.{suffix}\"\r\n"
        f"Content-Type: {media_type}\r\n\r\n"
    ).encode("ascii")
    payload = b"".join((fields, audio, f"\r\n--{boundary}--\r\n".encode("ascii")))
    response = _post_to_proxy(
        "/audio/transcriptions", key, payload, f"multipart/form-data; boundary={boundary}",
        {"application/json"}, 64 * 1024,
    )
    try:
        text = json.loads(response)["text"]
    except (KeyError, TypeError, ValueError):
        raise ProxyError from None
    if not isinstance(text, str):
        raise ProxyError
    return text.strip()


@router.post("/transcribe")
async def transcribe(request: Request):
    key = _require_voice()
    model = _model("HACK_STT_MODEL", "gpt-transcribe")
    media_type = _content_type(request)
    if media_type not in AUDIO_FORMATS:
        raise HTTPException(415, "Формат записи не поддерживается. Напишите просьбу в чате.")
    audio = await _bounded_body(request, MAX_AUDIO_BYTES)
    try:
        text = await run_in_threadpool(_transcribe, key, model, audio, media_type)
    except ProxyError:
        raise HTTPException(502, "Не удалось распознать запись. Напишите просьбу в чате.") from None
    if not text or len(text) > 1000:
        raise HTTPException(422, "Запишите короткую просьбу ещё раз или напишите её в чате.")
    return {"text": text}


@router.post("/speech")
async def speech(request: Request):
    key = _require_voice()
    model = _model("HACK_TTS_MODEL", "tts-1")
    if _content_type(request) != "application/json":
        raise HTTPException(415, "Для озвучивания нужен текст")
    try:
        text = SpeechRequest.model_validate_json(await _bounded_body(request, 4096)).text.strip()
    except ValidationError:
        raise HTTPException(422, "Передайте текст длиной до 500 символов") from None
    if not text:
        raise HTTPException(422, "Передайте текст для озвучивания")
    payload = json.dumps({
        "model": model,
        "input": text,
        "voice": "alloy",
        "response_format": "mp3",
    }, ensure_ascii=False).encode("utf-8")
    try:
        audio = await run_in_threadpool(
            _post_to_proxy, "/audio/speech", key, payload, "application/json", {"audio/mpeg"}, MAX_AUDIO_BYTES,
        )
    except ProxyError:
        raise HTTPException(502, "Не удалось озвучить ответ.") from None
    return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
