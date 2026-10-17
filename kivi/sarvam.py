"""Minimal Sarvam AI client (stdlib only): speech-to-text, chat, text-to-speech."""

import base64
import io
import json
import re
import urllib.error
import urllib.request
import uuid
import wave

API = "https://api.sarvam.ai"
TIMEOUT = 30

# STT language codes (saaras:v4). "unknown" = auto-detect.
LANGUAGES = {
    "unknown": "auto-detect",
    "en-IN": "English",
    "hi-IN": "Hindi",
    "bn-IN": "Bengali",
    "gu-IN": "Gujarati",
    "kn-IN": "Kannada",
    "ml-IN": "Malayalam",
    "mr-IN": "Marathi",
    "od-IN": "Odia",
    "pa-IN": "Punjabi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
    "as-IN": "Assamese",
    "ur-IN": "Urdu",
    "ne-IN": "Nepali",
    "kok-IN": "Konkani",
    "ks-IN": "Kashmiri",
    "sd-IN": "Sindhi",
    "sa-IN": "Sanskrit",
    "sat-IN": "Santali",
    "mni-IN": "Manipuri",
    "brx-IN": "Bodo",
    "mai-IN": "Maithili",
    "doi-IN": "Dogri",
}

# bulbul:v3 voices, from docs.sarvam.ai (shubh is the default).
TTS_SPEAKERS = [
    "shubh", "aditya", "ritu", "priya", "neha", "rahul", "pooja", "rohan", "simran", "kavya",
    "amit", "dev", "ishita", "shreya", "ratan", "varun", "manan", "sumit", "roopa", "kabir",
    "aayan", "ashutosh", "advait", "anand", "tanya", "tarun", "sunny", "mani", "gokul", "vijay",
    "shruti", "suhani", "mohit", "kavitha", "rehan", "soham", "rupali",
]

# Languages bulbul:v3 can speak.
TTS_LANGUAGES = {code: LANGUAGES[code] for code in (
    "en-IN", "hi-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN")}

TTS_CHUNK = 500


class SarvamError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would otherwise forward the api key headers to any host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(path, api_key, body, content_type, *, bearer=False):
    if not api_key:
        raise SarvamError("no sarvam api key set")
    headers = {"api-subscription-key": api_key, "Content-Type": content_type}
    if bearer:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(API + path, body, headers, method="POST")
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            err = json.loads(raw).get("error", raw)
            msg = err.get("message", raw) if isinstance(err, dict) else str(err)
        except (ValueError, AttributeError):
            msg = raw
        raise SarvamError(f"sarvam {e.code}: {msg.strip()[:300] or e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise SarvamError(f"can't reach sarvam: {reason}") from None
    except ValueError:
        raise SarvamError("sarvam returned an unreadable response") from None


def transcribe(api_key, wav_path, *, model="saaras:v4", language="unknown", keyterms=()) -> tuple[str, str | None]:
    fields = {"model": model, "language_code": language or "unknown"}
    terms = [t[:64] for t in keyterms if t][:50]
    if terms and model == "saaras:v4":
        fields["keyterms"] = json.dumps(terms, ensure_ascii=False)
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                 "Content-Type: audio/wav\r\n\r\n".encode())
    with open(wav_path, "rb") as f:
        parts.append(f.read())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    data = _post("/speech-to-text", api_key, b"".join(parts), f"multipart/form-data; boundary={boundary}")
    return (data.get("transcript") or "").strip(), data.get("language_code")


def _clean(text):
    return re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S).strip()


def chat(api_key, model, messages, *, tools=None, temperature=0.2, max_tokens=1024) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": None,  # hidden reasoning costs ~6 s and doesn't help here
    }
    if tools:
        body["tools"] = tools
    data = _post("/v1/chat/completions", api_key, json.dumps(body).encode(), "application/json", bearer=True)
    try:
        message = dict(data["choices"][0]["message"])
    except (KeyError, IndexError, TypeError):
        raise SarvamError("sarvam returned no answer") from None
    message["content"] = _clean(message.get("content"))
    return message


def complete(api_key, model, system, user) -> str:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return chat(api_key, model, messages)["content"]


def _chunks(text, limit=TTS_CHUNK):
    """Split on sentence (then word) boundaries into pieces of at most `limit` chars."""
    text = " ".join(text.split())
    out, cur = [], ""
    for sentence in re.split(r"(?<=[.!?।])\s+", text):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if cur and len(cur) + 1 + len(sentence) > limit:
            out.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        out.append(cur)
    return [c for c in out if c]


def _join_wavs(blobs):
    if len(blobs) == 1:
        return blobs[0]
    params, frames = None, []
    for b in blobs:
        with wave.open(io.BytesIO(b)) as w:
            params = params or w.getparams()
            frames.append(w.readframes(w.getnframes()))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(b"".join(frames))
    return buf.getvalue()


def tts(api_key, text, *, language="en-IN", speaker="shubh", model="bulbul:v3") -> bytes:
    pieces = _chunks(text)
    if not pieces:
        raise SarvamError("nothing to say")
    blobs = []
    for piece in pieces:
        body = {"text": piece, "target_language_code": language, "model": model, "speaker": speaker}
        data = _post("/text-to-speech", api_key, json.dumps(body).encode(), "application/json")
        audios = data.get("audios") or []
        if not audios:
            raise SarvamError("sarvam returned no audio")
        blobs.extend(base64.b64decode(a) for a in audios)
    return _join_wavs(blobs)


def check_key(api_key) -> tuple[bool, str]:
    if not api_key or not api_key.strip():
        return False, "no api key"
    try:
        chat(api_key.strip(), "sarvam-105b", [{"role": "user", "content": "say ok"}], max_tokens=4)
    except SarvamError as e:
        msg = str(e)
        if msg.startswith(("sarvam 401", "sarvam 403")):
            return False, "that key was rejected by sarvam"
        return False, msg
    return True, "key works"
