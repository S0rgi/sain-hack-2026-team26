import io
import json
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi import HTTPException, Request

from app import voice


CONFIG = {
    "HACK_VOICE_ENABLED": "1",
    "LLM_PROVIDER": "sain",
    "LLM_API_KEY": "example-test-token",
}


def incoming(content_type, *chunks, content_length=None):
    source = iter(chunks)
    headers = [(b"content-type", content_type.encode("ascii"))]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))

    async def receive():
        chunk = next(source, None)
        return {"type": "http.request", "body": chunk or b"", "more_body": chunk is not None}

    return Request({"type": "http", "headers": headers}, receive)


def upstream(content_type, body):
    response = io.BytesIO(body)
    response.headers = Message()
    response.headers["Content-Type"] = content_type
    return response


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        config = patch.dict("os.environ", CONFIG, clear=True)
        limiter = patch("app.voice.wait_for_sain_slot")
        provider = patch.object(voice._opener, "open")
        for mock in (config, limiter, provider):
            self.addCleanup(mock.stop)
        config.start()
        self.limiter = limiter.start()
        self.provider = provider.start()

    async def test_voice_defaults_to_disabled_and_requires_the_organizer_proxy(self):
        bad_configs = (
            {"HACK_VOICE_ENABLED": ""},
            {"LLM_PROVIDER": "google"},
            {"LLM_BASE_URL": "https://bifrost.be-healthy.tech.example/v1"},
            {"LLM_BASE_URL": "https://bifrost.be-healthy.tech@other.example/v1"},
            {"LLM_BASE_URL": "http://bifrost.be-healthy.tech/v1"},
            {"LLM_API_KEY": ""},
        )
        for config in bad_configs:
            with self.subTest(config=config), patch.dict("os.environ", config), self.assertRaises(HTTPException) as error:
                self.assertFalse(voice.voice_enabled())
                await voice.transcribe(incoming("audio/webm", b"audio"))
            self.assertEqual(error.exception.status_code, 503)
        self.provider.assert_not_called()

    async def test_transcription_sends_supported_audio_as_multipart(self):
        self.provider.return_value = upstream(
            "application/json; charset=utf-8",
            json.dumps({"text": "  Сделай свет ярче.  "}).encode(),
        )
        self.assertTrue(voice.voice_enabled())
        result = await voice.transcribe(incoming("audio/webm;codecs=opus", b"voice-", b"recording"))
        self.assertEqual(result, {"text": "Сделай свет ярче."})
        self.limiter.assert_called_once_with()
        request = self.provider.call_args.args[0]
        self.assertEqual(request.full_url, voice.ORGANIZER_PROXY + "/audio/transcriptions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer example-test-token")
        self.assertIn("multipart/form-data;", request.get_header("Content-type"))
        self.assertIn(b'name="model"\r\n\r\ngpt-transcribe', request.data)
        self.assertIn(b'name="file"; filename="request.webm"', request.data)
        self.assertIn(b"\r\n\r\nvoice-recording\r\n", request.data)

    async def test_invalid_uploads_fail_before_contacting_the_provider(self):
        with patch.object(voice, "MAX_AUDIO_BYTES", 8):
            requests = [
                (incoming("application/json", b"voice"), 415),
                (incoming("audio/webm", b"", content_length=9), 413),
                (incoming("audio/mp4", b"first", b"second"), 413),
                (incoming("audio/ogg"), 400),
            ]
            for request, status in requests:
                with self.subTest(status=status), self.assertRaises(HTTPException) as error:
                    await voice.transcribe(request)
                self.assertEqual(error.exception.status_code, status)
        self.provider.assert_not_called()

    async def test_proxy_error_body_and_redirects_are_not_returned(self):
        self.provider.side_effect = HTTPError(
            voice.ORGANIZER_PROXY + "/audio/transcriptions", 403,
            "provider-private-data", {}, io.BytesIO(b"provider-private-data"),
        )
        with self.assertRaises(HTTPException) as error:
            await voice.transcribe(incoming("audio/webm", b"sample"))
        self.assertEqual(error.exception.status_code, 502)
        self.assertNotIn("provider-private-data", error.exception.detail)
        redirect = next(handler for handler in voice._opener.handlers if isinstance(handler, voice._NoRedirect))
        self.assertIsNone(redirect.redirect_request(None, None, 307, "", {}, "https://other.example"))

    async def test_audio_response_is_bounded_and_cached_nowhere(self):
        self.provider.return_value = upstream("audio/mpeg", b"spoken-result")
        response = await voice.speech(incoming("application/json", '{"text":"Уже светлее."}'.encode()))
        self.assertEqual(response.headers["Content-Type"], "audio/mpeg")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.body, b"spoken-result")
        request = self.provider.call_args.args[0]
        self.assertEqual(request.full_url, voice.ORGANIZER_PROXY + "/audio/speech")
        self.assertEqual(json.loads(request.data), {
            "model": "tts-1", "input": "Уже светлее.", "voice": "alloy", "response_format": "mp3",
        })

        self.provider.return_value = upstream("audio/mpeg", b"x" * 11)
        with patch.object(voice, "MAX_AUDIO_BYTES", 10), self.assertRaises(HTTPException) as too_large:
            await voice.speech(incoming("application/json", b'{"text":"done"}'))
        self.assertEqual(too_large.exception.status_code, 502)

    async def test_speech_rejects_blank_and_large_text(self):
        for text in ["   ", "x" * 501]:
            with self.subTest(length=len(text)), self.assertRaises(HTTPException) as error:
                await voice.speech(incoming("application/json", json.dumps({"text": text}).encode()))
            self.assertEqual(error.exception.status_code, 422)
        self.provider.assert_not_called()
