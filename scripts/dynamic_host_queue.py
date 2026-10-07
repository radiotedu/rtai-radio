from __future__ import annotations

import hashlib
import difflib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import wave
from array import array
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    from .metadata_cleanup import clean_catalog_name
except ImportError:
    from metadata_cleanup import clean_catalog_name

ROUTINE_OPENERS = (
    "good morning", "good afternoon", "good evening", "hello everyone",
    "bonjour à tous", "bonsoir à tous", "salut tout le monde",
)
SLOP_PHRASES = (
    "sonic journey", "musical journey", "soundscape", "vibrant tapestry",
    "let's dive", "dive into", "delve into", "captivating", "mesmerizing",
    "elevate your", "where music meets", "the perfect soundtrack", "vibe check",
    "voyage sonore", "paysage sonore", "plongeons", "captivant", "envoûtant",
    "is back", "we're playing", "we are playing", "est de retour", "nous jouons",
    "once said", "once back-announced", "back-announced the previous",
    "introduced the next", "the previous track", "the next song",
    "catalog value", "radio host", "this sentence",
    "previous title", "next title", "previous artist", "next artist", "station:",
    "a dit une fois", "a annoncé le titre précédent", "a présenté le titre suivant",
    "le morceau précédent", "la chanson suivante", "valeur du catalogue",
)

# English and French share one small local model. Serializing generation keeps
# the two video encoders from competing with simultaneous Ollama requests.
MODEL_GENERATION_LOCK = threading.Lock()
ISTANBUL = ZoneInfo("Europe/Istanbul")
SPOKEN_STATION_NAME = "Radio Ted You"
# Kokoro receives the expanded spelling so the public brand is heard as
# “Ted” (rhymes with “bed”) followed by “you”, matching rtai-jingle.
TTS_STATION_NAME = "Radio Ted You"
TTS_MODEL_NAME = "Kokoro-82M"
TTS_VOICE_VERSION = "kokoro-82m-michael-onyx-siwis-no-nonverbal-v1"
QWEN_TEXT_MODEL = "qwen3:0.6b"
AUDIO_QUALITY_VERSION = "complete-speech-v4-kokoro-no-sigh"
TTS_QUALITY_ATTEMPTS = 3
QUEUE_STATE_VERSION = 12
ANNOUNCEMENT_COPY_VERSION = "varied-full-script-v1"
# Evergreen liners pre-warm Kokoro but are now probabilistically varied human
# micro-breaks so every boundary sounds improvised. Pool is durable and reused.
LINER_POOL_TARGET = 6
LINERS = {
    "en": (
        "This is Radio TED U, with another song on the way.",
        "You are listening to Radio TED U, your home for music all night.",
        "Keep it right here, because Radio TED U has more music next.",
        "Radio TED U continues right now with the music you love.",
        "Stay tuned to Radio TED U, the station that plays your songs.",
        "More music is coming up next, here on Radio TED U.",
        "Right here on Radio TED U — more music just ahead.",
        "Radio TED U keeps the music flowing for you.",
        "On Radio TED U, the next sound is already lining up.",
        "Thanks for staying with Radio TED U — here's what's next.",
        "Late night or not, Radio TED U has your next song ready.",
        "You're with Radio TED U, let's keep the music going.",
    ),
    "fr": (
        "Ici Radio TED U, avec encore de la musique pour vous.",
        "Vous écoutez Radio TED U, votre radio pour la bonne musique.",
        "Restez avec nous, Radio TED U enchaîne avec de la musique.",
        "Encore de la musique vous attend, ici sur Radio TED U.",
        "Radio TED U est là pour vous, avec la musique que vous aimez.",
        "Ne quittez pas Radio TED U, la musique continue après ce titre.",
        "Toujours sur Radio TED U, la musique continue.",
        "Radio TED U vous accompagne, encore un titre arrive.",
        "Vous êtes sur Radio TED U, on continue en musique.",
        "Merci d'écouter Radio TED U — la suite arrive.",
        "Radio TED U reste avec vous toute la soirée.",
        "Ici Radio TED U, la prochaine chanson arrive tout de suite.",
    ),
}


def atomic_text_replace(path: Path, payload: str) -> None:
    """Persist small queue metadata without Windows temp-file collisions."""
    last_error: OSError | None = None
    for attempt in range(20):
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{attempt}.{time.monotonic_ns()}.tmp"
        )
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)
            return
        except OSError as exc:
            last_error = exc
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            transient = (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in {5, 32, 33}
                or getattr(exc, "errno", None) in {13, 16, 32}
            )
            if not transient or attempt >= 19:
                break
            time.sleep(min(0.05 * (attempt + 1), 0.4))
    if last_error is not None:
        raise last_error
    raise OSError(f"unable to persist {path}")


def current_daypart(now: datetime | None = None) -> str:
    local = now.astimezone(ISTANBUL) if now is not None else datetime.now(ISTANBUL)
    if local.hour >= 18 or local.hour < 6:
        return "night"
    if local.hour < 10:
        return "morning"
    return "daytime"


def normalize_station_name(text: str) -> str:
    return re.sub(
        r"\bRadio\s*TED(?:\s*[-–—]?\s*)?U\b",
        SPOKEN_STATION_NAME,
        text,
        flags=re.IGNORECASE,
    )


def normalize_tts_text(text: str) -> str:
    """Expand the station brand only in text submitted to speech synthesis."""
    return text.replace(SPOKEN_STATION_NAME, TTS_STATION_NAME)


@dataclass(frozen=True)
class QueuedHostClip:
    path: Path
    sequence: int


def valid_line(value: object, language: str | None = None) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip('"')
    words = text.split()
    # Catalog titles can legitimately include a long classical work or its
    # movement. Allow a bounded radio link to name both tracks and artists;
    # Kokoro's WAV gate still caps the resulting clip at 45 seconds.
    if not 6 <= len(words) <= 44:
        return None
    if any(text.casefold().startswith(opener) for opener in ROUTINE_OPENERS):
        return None
    if any(character in text for character in "{}[]"):
        return None
    if any(phrase in text.casefold() for phrase in SLOP_PHRASES):
        return None
    lowered_words = {word.strip(".,!?;:()—-").casefold() for word in words}
    if language == "en" and not lowered_words.intersection(
        {
            "a", "and", "are", "by", "can", "in", "is", "it", "next", "of",
            "on", "that", "the", "this", "to", "was", "we", "with", "you", "your",
        }
    ):
        return None
    if language == "fr" and not lowered_words.intersection(
        {"à", "avec", "ce", "cette", "dans", "de", "des", "du", "est", "et", "la", "le", "les", "nous", "un", "une", "vous"}
    ):
        return None
    return text


def _probabilistic_choice(sequence: int, seed: int, count: int) -> int:
    """Deterministic-per-sequence but varied across catalogue: seed + sequence shuffle."""
    import random as _rand
    h = hashlib.sha256(f"{seed}:{sequence}".encode("utf-8")).digest()
    rnd = _rand.Random(int.from_bytes(h[:8], "big"))
    return rnd.randrange(count)


def grounded_radio_link(
    language: str,
    sequence: int,
    *,
    previous_title: str,
    previous_artist: str,
    next_title: str,
    next_artist: str,
    seed: int = 0,
    daypart: str | None = None,
) -> str:
    """Probabilistically varied but strictly catalog-grounded link — no invented facts."""
    daypart = (daypart or "daytime").casefold()
    if language == "fr":
        base_templates = (
            "C'était {previous_title}, de {previous_artist}. Sur {station}, voici {next_title}, de {next_artist}.",
            "{previous_title}, de {previous_artist}, sur {station}. Maintenant, {next_title}, de {next_artist}.",
            "Vous venez d'entendre {previous_title}, de {previous_artist}. {station} enchaîne avec {next_title}, de {next_artist}.",
            "{previous_title}, de {previous_artist}, vient de se terminer. Ici {station}; place à {next_title}, de {next_artist}.",
            "C'était {previous_title}, signé {previous_artist}. Sur {station}, la suite: {next_title}, de {next_artist}.",
            "Vous étiez avec {previous_artist} et {previous_title}. Sur {station}, voici {next_title}, de {next_artist}.",
            "{previous_artist} avec {previous_title}, à l'instant. {station} continue avec {next_artist} et {next_title}.",
            "Fin de {previous_title}, de {previous_artist}. {station}: {next_title}, de {next_artist}.",
            "À l'instant {previous_title} de {previous_artist}. {station} vous offre {next_title}, de {next_artist}.",
            "{previous_title} — {previous_artist} — c'était à l'instant. Voici {next_title} de {next_artist}, sur {station}.",
            "On garde le rythme sur {station}: {previous_title} de {previous_artist} laisse place à {next_title}, de {next_artist}.",
            "Vous écoutiez {previous_title} par {previous_artist}. {station} poursuit avec {next_title} de {next_artist}.",
            "{previous_artist} et {previous_title} à l'instant. {station} envoie {next_title} de {next_artist}.",
            "Merci d'être sur {station}: après {previous_title} de {previous_artist}, voici {next_title} de {next_artist}.",
            "Nuit ou jour, {station} enchaîne: {previous_title} de {previous_artist}, puis {next_title} de {next_artist}.",
            "{previous_title} de {previous_artist} s'achève. Place au suivant sur {station}: {next_title}, de {next_artist}.",
        )
        # Night adds a softer connective 20% of the time via deterministic coin
        if daypart == "night" and _probabilistic_choice(sequence, seed, 5) == 0:
            night_templates = (
                "Encore douce soirée sur {station}: {previous_title} de {previous_artist} s'éloigne, voici {next_title} de {next_artist}.",
                "Nuit tranquille sur {station} — merci pour {previous_title} de {previous_artist}. Place à {next_title}, de {next_artist}.",
            )
            templates = base_templates + night_templates
        else:
            templates = base_templates
    else:
        base_templates = (
            "That was {previous_title} by {previous_artist}. On {station}, next is {next_title} by {next_artist}.",
            "{previous_title} by {previous_artist}, on {station}. Now, {next_title} by {next_artist}.",
            "You just heard {previous_title} by {previous_artist}. {station} continues with {next_title} by {next_artist}.",
            "{previous_title} by {previous_artist} just finished. This is {station}; next, {next_title} by {next_artist}.",
            "That was {previous_artist} with {previous_title}. On {station}: {next_title} by {next_artist}.",
            "You were listening to {previous_title} by {previous_artist}. {station} now brings you {next_title} by {next_artist}.",
            "{previous_artist} with {previous_title}, just now. {station} continues with {next_artist} and {next_title}.",
            "That closes {previous_title} by {previous_artist}. {station}: {next_title} by {next_artist}.",
            "From {previous_title} by {previous_artist} — now on {station}, {next_title} by {next_artist}.",
            "{previous_title} — {previous_artist} — just there. Up next on {station}: {next_title} by {next_artist}.",
            "Still with {station}, that was {previous_title} by {previous_artist}. Here's {next_title} by {next_artist}.",
            "Thanks for staying on {station}: after {previous_title} by {previous_artist}, here's {next_title} by {next_artist}.",
            "{previous_artist} — {previous_title} — fades out. {station} brings in {next_title} by {next_artist}.",
            "You had {previous_title} from {previous_artist}. {station} keeps it moving with {next_title} by {next_artist}.",
            "That was {previous_title}. {previous_artist} on {station}. Next, {next_title} — {next_artist}.",
            "Keeping it flowing on {station}: {previous_title} by {previous_artist}, now {next_title} by {next_artist}.",
        )
        if daypart == "night" and _probabilistic_choice(sequence, seed + 101, 5) == 0:
            night_templates = (
                "Late night on {station}: {previous_title} by {previous_artist} drifts out — here's {next_title} by {next_artist}.",
                "Easy night with {station}: thanks for {previous_title} by {previous_artist}, here's {next_title} by {next_artist}.",
            )
            templates = base_templates + night_templates
        else:
            templates = base_templates
    # Deterministic pseudo-random selection keeps each context cacheable while
    # avoiding a fixed modulo cadence that sounds repetitive on air.
    idx = _probabilistic_choice(
        sequence, seed + (17 if language == "fr" else 0), len(templates)
    )
    return templates[idx].format(
        previous_title=previous_title,
        previous_artist=previous_artist,
        next_title=next_title,
        next_artist=next_artist,
        station=SPOKEN_STATION_NAME,
    )


def _normalized_words(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", str(value or "").casefold(), flags=re.UNICODE))


def _contains_grounded_value(line: str, value: str) -> bool:
    haystack = f" {_normalized_words(line)} "
    needle = _normalized_words(value)
    return bool(needle and f" {needle} " in haystack)


def announcement_script(line: str, **facts: str) -> str:
    """Strip catalog facts before comparing wording across different songs."""
    for key, value in sorted(facts.items(), key=lambda pair: len(pair[1]), reverse=True):
        if value:
            line = re.sub(re.escape(value), lambda _match: "{" + key + "}", line, flags=re.I)
    return line


def repeated_script(script: str, recent_scripts: tuple[str, ...]) -> bool:
    signature = _normalized_words(script)
    opener = signature.split()[:5]
    for previous in recent_scripts[-12:]:
        other = _normalized_words(previous)
        if signature == other or difflib.SequenceMatcher(None, signature, other).ratio() >= 0.84:
            return True
        if len(opener) == 5 and opener == other.split()[:5]:
            return True
    return False


def _well_wish_due(sequence: int) -> bool:
    """Add a short listener wish to every third announcement."""
    return (int(sequence) + 1) % 3 == 0


def _bridge_choices(language: str, wish_due: bool, daypart: str | None) -> tuple[str, ...]:
    night = (daypart or "daytime").casefold() == "night"
    if language == "fr":
        if wish_due:
            return (
                "Je vous souhaite une belle soirée." if night else "Je vous souhaite une belle journée.",
                "Passez une très belle soirée." if night else "Passez une très belle journée.",
                "Je vous souhaite une soirée lumineuse." if night else "Je vous souhaite une journée lumineuse.",
                "Prenez bien soin de vous, où que vous soyez.",
                "Merci de partager ce moment avec nous.",
                "J'espère que la musique vous tient bonne compagnie.",
                "Je vous souhaite un petit moment de détente.",
                "Un peu de douceur pour votre soirée." if night else "Un peu de douceur pour votre journée.",
                "J'espère que vous trouverez une occasion de sourire.",
                "Prenez un moment pour vous, si vous le pouvez.",
                "Que votre soirée soit paisible." if night else "Que votre journée soit agréable.",
            )
        return (
            "Encore de la musique à suivre.",
            "Restez avec nous.",
            "Continuons en musique.",
            "La suite arrive.",
        )
    if wish_due:
        return (
            "Hope you have a lovely evening." if night else "Hope you have a lovely day.",
            "Wishing you a beautiful evening." if night else "Wishing you a beautiful day.",
            "Hope your evening is going well." if night else "Hope your day is going well.",
            "Wherever you're listening, take good care of yourself.",
            "Hope there's something good in your day today.",
            "Thanks for spending a little of your time with us.",
            "Here's wishing you a little time to unwind.",
            "Sending a little warmth your way.",
            "Hope the music keeps you good company.",
            "Take a moment for yourself, if you can.",
            "Wishing you a peaceful evening." if night else "Wishing you an easy afternoon.",
            "Hope you find a reason to smile today.",
        )
    return (
        "More music is coming up.",
        "Stay with us for more.",
        "Here comes another song.",
        "Keep it right here.",
    )


def _grounded_well_wish_link(
    language: str,
    *,
    previous_title: str,
    previous_artist: str,
    next_title: str,
    next_artist: str,
    daypart: str | None,
) -> str:
    station = SPOKEN_STATION_NAME
    if language == "fr":
        wish = "Je vous souhaite une belle soirée." if daypart == "night" else "Je vous souhaite une belle journée."
        return (
            f"C'était {previous_title}, de {previous_artist}. Sur {station}, "
            f"voici {next_title}, de {next_artist}. {wish}"
        )
    wish = "Hope your evening is going well." if daypart == "night" else "Hope your day is going well."
    return (
        f"That was {previous_title} by {previous_artist}. On {station}, "
        f"here is {next_title} by {next_artist}. {wish}"
    )


def _ollama_radio_link(
    language: str,
    ollama_url: str,
    model: str,
    sequence: int,
    seed: int,
    *,
    previous_title: str,
    previous_artist: str,
    next_title: str,
    next_artist: str,
    daypart: str | None,
    recent_scripts: tuple[str, ...] = (),
) -> str:
    wish_due = _well_wish_due(sequence)
    wishes = _bridge_choices(language, True, daypart)
    available_wishes = [wish for wish in wishes if not any(wish.casefold() in old.casefold() for old in recent_scripts[-8:])]
    wish = available_wishes[(sequence + seed) % len(available_wishes)] if wish_due and available_wishes else (wishes[sequence % len(wishes)] if wish_due else "")
    prompt_data = {
        "language": "English" if language == "en" else "French",
        "daypart": daypart or "daytime",
        "include_well_wish": wish_due,
        "closing": wish,
        "previous_title": previous_title,
        "previous_artist": previous_artist,
        "next_title": next_title,
        "next_artist": next_artist,
        "station": SPOKEN_STATION_NAME,
        "recent_scripts_to_avoid": list(recent_scripts[-4:]),
        "style": ("brief and conversational", "artist first", "station between the songs", "a quick handoff", "thank the listener", "title first")[sequence % 6],
    }
    request_payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a radio presenter. Write two short spoken sentences: name what just played, "
                    "then introduce what is up next. Copy the names exactly. Mention the station once. "
                    "Use fresh phrasing. No trivia, labels or wishes. Return JSON with only script."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Language: {prompt_data['language']}. Just played: {previous_title} by {previous_artist}. "
                    f"Up next: {next_title} by {next_artist}. Station: {SPOKEN_STATION_NAME}. "
                    f"Style: {prompt_data['style']}. "
                    + ("Avoid these recent scripts: " + json.dumps(prompt_data["recent_scripts_to_avoid"], ensure_ascii=False) if recent_scripts else "")
                ),
            },
        ],
        "think": False,
        "format": {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"], "additionalProperties": False},
        "stream": False,
        "keep_alive": "10m",
        "options": {
            "temperature": 0.65,
            "top_p": 0.92,
            "top_k": 40,
            "repeat_penalty": 1.15,
            "num_ctx": 2048,
            "num_predict": 150,
            "seed": int((int(seed) + int(sequence) * 104_729) % 2_147_483_647),
        },
    }
    request = Request(
        ollama_url.rstrip("/") + "/api/chat",
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read(256 * 1024).decode("utf-8"))
    content = str(((payload.get("message") or {}).get("content")) or "").strip()
    try:
        decoded = json.loads(content)
        script = str(decoded.get("script") or "").strip() if isinstance(decoded, dict) else ""
    except (TypeError, ValueError, json.JSONDecodeError):
        script = ""
    script = re.sub(r"\s+", " ", script).strip()
    facts = dict(previous_title=previous_title, previous_artist=previous_artist,
                 next_title=next_title, next_artist=next_artist, station=SPOKEN_STATION_NAME)
    # A literal-placeholder script is also accepted for constrained runtimes;
    # normally Qwen writes the full prose with the supplied catalog names.
    if "{" not in script:
        if not all(_contains_grounded_value(script, value) for value in facts.values()):
            raise ValueError("text model omitted or changed catalog names")
        script = announcement_script(script, **facts)
    tokens = re.findall(r"\{([^{}]+)\}", script)
    # One artist can appear in both adjacent songs. Replacement may then use
    # the same artist placeholder twice; preserve the actual catalog identity.
    if previous_artist.casefold() == next_artist.casefold():
        script = script.replace("{previous_artist}", "{same_artist}").replace("{next_artist}", "{same_artist}")
        script = script.replace("{same_artist}", "{previous_artist}", 1).replace("{same_artist}", "{next_artist}", 1)
        tokens = re.findall(r"\{([^{}]+)\}", script)
    if sorted(tokens) != sorted(facts) or script.count("{") != 5 or script.count("}") != 5:
        raise ValueError("text model omitted or changed song placeholders")
    if max(script.index("{previous_title}"), script.index("{previous_artist}")) > min(script.index("{next_title}"), script.index("{next_artist}")):
        raise ValueError("text model reversed adjacent songs")
    prose = re.sub(r"\{[^{}]+\}", "", script)
    if len(prose.split()) > 24 or re.search(r"\d|[\[\]]|\w_\w|\b(released|recorded|album|classic|legendary|award|born|genre|sorti|album|légendaire|placeholder|labels|script)\b|closing|as requested|station name|artist names|spoken sentences|fresh wording|added separately", prose, re.I):
        raise ValueError("text model added unsupported song claims")
    if re.search(r"\b(hope|wishing|lovely|beautiful|souhaite|belle journée|belle soirée)\b", prose, re.I):
        raise ValueError("text model added an unscheduled wish")
    script = script + (" " + wish if wish else "")
    if repeated_script(script, recent_scripts):
        raise ValueError("text model repeated recent announcement wording")
    line = script.format(**facts)
    if not all(
        _contains_grounded_value(line, value)
        for value in (previous_title, previous_artist, next_title, next_artist)
    ) or not valid_line(line, language):
        raise ValueError("assembled radio link is invalid")
    return line


def generate_line(
    language: str,
    ollama_url: str,
    model: str,
    sequence: int,
    seed: int,
    *,
    previous_title: str = "",
    previous_artist: str = "",
    next_title: str = "",
    next_artist: str = "",
    daypart: str | None = None,
    recent_scripts: tuple[str, ...] = (),
    generation_details: dict[str, str] | None = None,
) -> str:
    # Small local Qwen3 writes every song link. A strictly grounded template is
    # kept as a safety net so a sleeping or unavailable text runtime never
    # removes a prepared announcement from the rolling queue.
    if model != QWEN_TEXT_MODEL:
        raise RuntimeError(f"radio links require {QWEN_TEXT_MODEL}")
    if previous_title and previous_artist:
        try:
            with MODEL_GENERATION_LOCK:
                on_air_line = _ollama_radio_link(
                    language,
                    ollama_url,
                    model,
                    sequence,
                    seed,
                    previous_title=previous_title,
                    previous_artist=previous_artist,
                    next_title=next_title,
                    next_artist=next_artist,
                    daypart=daypart,
                    recent_scripts=recent_scripts,
                )
                if generation_details is not None:
                    generation_details["copy_source"] = "qwen_generated_full_script"
        except Exception as exc:
            if generation_details is not None:
                generation_details.update(copy_source="grounded_variety_fallback", rejection=str(exc)[:240])
            facts = dict(previous_title=previous_title, previous_artist=previous_artist,
                         next_title=next_title, next_artist=next_artist, station=SPOKEN_STATION_NAME)
            wishes = _bridge_choices(language, True, daypart)
            available = [wish for wish in wishes if not any(wish.casefold() in old.casefold() for old in recent_scripts[-8:])]
            wish = (available or list(wishes))[(sequence + seed) % len(available or wishes)] if _well_wish_due(sequence) else ""
            # Search the complete template set against recent wording rather
            # than falling back to one recurring "That was ..." sentence.
            for attempt in range(128):
                candidate = grounded_radio_link(
                    language,
                    sequence + attempt,
                    previous_title=previous_title,
                    previous_artist=previous_artist,
                    next_title=next_title,
                    next_artist=next_artist,
                    seed=seed,
                    daypart=daypart,
                )
                candidate += " " + wish if wish else ""
                if valid_line(candidate, language) and not repeated_script(announcement_script(candidate, **facts), recent_scripts):
                    on_air_line = candidate
                    break
            else:
                raise RuntimeError("no fresh grounded announcement fits the speech limit")
    else:
        raise RuntimeError("a contextual radio link requires both adjacent songs")
    # Validate the fallback too; never speak an incomplete catalog record.
    if not valid_line(on_air_line, language):
        raise RuntimeError("radio link exceeds the safe spoken-copy limit")
    return on_air_line


def _synthesize(
    text: str,
    design_id: str,
    target: Path,
    *,
    language: str,
    qwen_url: str,
    synthesis_seed: int,
    priority: str,
) -> None:
    """Publish only a complete Kokoro WAV; retry the same local model if needed."""
    normalized_text = normalize_tts_text(normalize_station_name(text))
    last_error: Exception | None = None
    for attempt in range(TTS_QUALITY_ATTEMPTS):
        temporary = target.with_name(f".{target.stem}.{attempt}.part.wav")
        try:
            request = Request(
                qwen_url.rstrip("/") + "/v1/synthesize",
                data=json.dumps({
                    "text": normalized_text,
                    "language": language,
                    "design_id": design_id,
                    "seed": synthesis_seed + (attempt * 104_729),
                    "priority": priority,
                }, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "audio/wav"},
                method="POST",
            )
            # Kokoro is small enough for short local renders. Keep a finite
            # request bound; the music playout continues if a speech clip fails.
            with urlopen(request, timeout=180) as response:
                if response.headers.get("X-TTS-Engine") != "kokoro":
                    raise RuntimeError("speech response was not Kokoro")
                if response.headers.get("X-TTS-Fallback") != "false":
                    raise RuntimeError("speech response did not prove fallback was disabled")
                if response.headers.get("X-TTS-Complete") != "true":
                    raise RuntimeError("Kokoro did not prove complete speech rendering")
                if response.headers.get("X-TTS-Hit-Ceiling") != "false":
                    raise RuntimeError("Kokoro marked speech rendering as incomplete")
                try:
                    rendered_chunks = int(response.headers.get("X-TTS-Rendered-Chunks") or 0)
                except (TypeError, ValueError):
                    rendered_chunks = 0
                if rendered_chunks <= 0:
                    raise RuntimeError("Kokoro completion proof is invalid")
                temporary.write_bytes(response.read(20 * 1024 * 1024))
            rejection = completed_host_wav_rejection(temporary, normalized_text)
            if rejection is not None:
                quarantine_root = target.parent / "rejected"
                quarantine_root.mkdir(parents=True, exist_ok=True)
                rejected_audio = quarantine_root / (
                    f"{target.stem}-{int(time.time())}-attempt-{attempt + 1}.wav"
                )
                temporary.replace(rejected_audio)
                atomic_text_replace(
                    rejected_audio.with_suffix(".json"),
                    json.dumps(
                        {
                            "version": 1,
                            "reason": rejection,
                            "language": language,
                            "design_id": design_id,
                            "text": text,
                            "tts_engine": "kokoro",
                            "tts_model": TTS_MODEL_NAME,
                            "rendered_chunks": rendered_chunks,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                raise RuntimeError(rejection)
            temporary.replace(target)
            return
        except Exception as exc:
            last_error = exc
        finally:
            temporary.unlink(missing_ok=True)
    raise RuntimeError(
        "Kokoro produced no completed host audio after "
        f"{TTS_QUALITY_ATTEMPTS} local attempts: {last_error}"
    )


def normalize_spoken_metadata(value: str) -> str:
    """Remove catalog-only video labels and bracket punctuation from spoken names."""
    text = clean_catalog_name(value)

    def clean_bracket(match: re.Match[str]) -> str:
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", label):
            return " "
        if re.search(
            r"\b(?:official|video|lyrics|audio|4k|hd|upgrade)\b",
            label,
            flags=re.IGNORECASE,
        ):
            return " "
        return f" {label} "

    text = re.sub(r"\[([^\]]*)\]", clean_bracket, text)
    text = re.sub(
        r"\s*\((?=[^)]*\b(?:official|video|lyrics|audio|4k|hd|upgrade)\b)[^)]*\)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = text.translate(str.maketrans({"[": " ", "]": " ", "{": " ", "}": " "}))
    return re.sub(r"\s+", " ", text).strip(" -_")


def safe_spoken_metadata(*values: str) -> bool:
    """Return true only for complete metadata that is safe to read aloud."""
    unsafe = ("anonymous", "unknown", "unspecified", "untitled")
    for raw in values:
        value = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not value or len(value) > 160:
            return False
        lowered = value.casefold()
        if lowered in unsafe or re.fullmatch(r"(?:anonymous\d*|unknown\s*artist)", lowered):
            return False
        if any(ord(character) < 32 for character in value):
            return False
        if any("\uff00" <= character <= "\uffef" for character in value):
            return False
        if not any(character.isalpha() for character in value) and not re.fullmatch(r"\d{3,}", value):
            return False
    return True


def valid_mono_wav(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 1024:
            return False
        with wave.open(str(path), "rb") as wav:
            return bool(
                wav.getnchannels() == 1
                and wav.getnframes() > 0
                and 16000 <= wav.getframerate() <= 48000
            )
    except (OSError, EOFError, wave.Error):
        return False


def _pcm_rms(samples: array) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(int(value) * int(value) for value in samples) / len(samples))


def completed_host_wav_rejection(path: Path, text: str) -> str | None:
    """Return a rejection reason when a PCM host link looks cut or degenerate."""
    try:
        if not path.is_file() or path.stat().st_size <= 1024:
            return "Kokoro generated empty host audio"
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frame_count = wav.getnframes()
            compression = wav.getcomptype()
            raw = wav.readframes(frame_count)
        if (
            channels != 1
            or sample_width != 2
            or compression != "NONE"
            or not 16000 <= sample_rate <= 48000
            or frame_count <= 0
        ):
            return "Kokoro WAV is not broadcast-safe mono PCM16"
        if len(raw) != frame_count * sample_width:
            return "Kokoro generated a truncated WAV payload"
        samples = array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()
        duration = frame_count / sample_rate
        words = re.findall(r"[^\W_]+(?:['’][^\W_]+)?", text, flags=re.UNICODE)
        letters = sum(character.isalnum() for character in text)
        minimum_duration = max(1.2, len(words) / 4.8, letters / 38.0)
        # Neutral Kokoro voices speak at a brisk radio pace. This floor removes
        # empty or truncated output while allowing a short, complete sentence.
        # WAV/tokenizer framing can add a fraction of a second beyond the
        # linguistic estimate. A 0.75s tolerance accepts that boundary noise
        # without admitting the 21–41s distressed outputs seen in commission.
        maximum_duration = min(
            45.0,
            max(8.0, len(words) / 0.9 + 3.0) + 0.75,
        )
        if duration < minimum_duration:
            return (
                "Kokoro audio ended too early for the complete text "
                f"({duration:.2f}s < {minimum_duration:.2f}s)"
            )
        if duration > maximum_duration:
            return (
                "Kokoro audio ran beyond the safe speech duration "
                f"({duration:.2f}s > {maximum_duration:.2f}s)"
            )
        clipped = sum(1 for value in samples if abs(value) >= 32_700)
        if clipped / len(samples) > 0.005:
            return "Kokoro audio contains excessive clipping"
        window_frames = max(1, int(sample_rate * 0.04))
        windows = [
            _pcm_rms(samples[index:index + window_frames])
            for index in range(0, len(samples), window_frames)
        ]
        if not windows:
            return "Kokoro audio has no analyzable speech"
        ranked = sorted(windows)
        speech_level = ranked[min(len(ranked) - 1, int(len(ranked) * 0.85))]
        if speech_level < 300:
            return "Kokoro audio contains no clear speech"
        tail_frames = min(len(samples), max(1, int(sample_rate * 0.20)))
        final_frames = min(len(samples), max(1, int(sample_rate * 0.06)))
        tail_rms = _pcm_rms(samples[-tail_frames:])
        final_rms = _pcm_rms(samples[-final_frames:])
        # Natural Kokoro output can end on a soft consonant without appending
        # silence. The codec-token header proves natural EOS; this waveform gate
        # only rejects a plainly abrupt, full-level endpoint.
        if (
            tail_rms > max(900.0, speech_level * 0.70)
            and final_rms > max(1_100.0, speech_level * 0.80)
        ):
            return "Kokoro audio has an active, unfinished speech tail"
        return None
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        return f"Kokoro generated an unreadable WAV: {exc}"


class DynamicHostQueue:
    """Maintain a durable rolling queue of ephemeral host segments."""

    def __init__(
        self,
        *,
        language: str,
        voice_design: str,
        night_voice_design: str,
        qwen_url: str,
        model: str,
        ollama_url: str,
        root: Path,
        stop: threading.Event,
        lead_min: int = 3,
        lead_max: int = 5,
        queue_target: int = 10,
        startup_min: int = 0,
        rolling_prepare: int = 3,
        seed: int = 20260813,
    ) -> None:
        self.language = language
        self.voice_design = voice_design
        self.night_voice_design = night_voice_design
        self.qwen_url = qwen_url
        self.model = model
        self.ollama_url = ollama_url
        self.root = root
        self.stop = stop
        self.lead_min = lead_min
        self.lead_max = lead_max
        self.queue_target = max(1, int(queue_target))
        self.startup_min = max(0, min(int(startup_min), self.queue_target))
        self.rolling_prepare = max(1, min(int(rolling_prepare), self.queue_target))
        self.seed = seed
        self.lock = threading.RLock()
        self.copy_lock = threading.Lock()
        self.recent_scripts: deque[str] = deque(maxlen=24)
        try:
            history = json.loads((root / "announcement-history.json").read_text(encoding="utf-8"))
            self.recent_scripts.extend(str(value) for value in history.get("scripts", [])[-24:])
        except (OSError, ValueError, TypeError):
            pass
        self.ready: deque[QueuedHostClip] = deque()
        self.songs_remaining: int | None = None
        self.last_error = ""
        self.missed_boundary_count = 0
        self.stale_boundary_count = 0
        self.last_boundary_failure: dict[str, object] = {}
        self.prepared_boundary_count = 0
        self.preparing_sequences: set[int] = set()
        self.preparing_contexts: dict[int, str] = {}
        self.prepared_sequences: set[int] = set()
        self.prepared_contexts: dict[int, str] = {}
        self.last_prepared_line = ""
        self.context_threads: dict[int, threading.Thread] = {}
        self.thread = threading.Thread(
            target=self._produce,
            name=f"dynamic-host-{language}",
            daemon=True,
        )
        self.pool_thread: threading.Thread | None = None
        self._pool_cursor = 0

    @property
    def _sequence_path(self) -> Path:
        return self.root / "sequence.json"

    @property
    def _queue_path(self) -> Path:
        return self.root / "queue.json"

    @property
    def preparing_sequence(self) -> int | None:
        return min(self.preparing_sequences) if self.preparing_sequences else None

    @preparing_sequence.setter
    def preparing_sequence(self, value: int | None) -> None:
        self.preparing_sequences.clear()
        if value is not None:
            self.preparing_sequences.add(value)

    @property
    def prepared_sequence(self) -> int | None:
        if self.ready and self.ready[0].sequence in self.prepared_sequences:
            return self.ready[0].sequence
        return None

    @prepared_sequence.setter
    def prepared_sequence(self, value: int | None) -> None:
        self.prepared_sequences.clear()
        if value is not None:
            self.prepared_sequences.add(value)

    def _next_sequence(self) -> int:
        sequence = 0
        try:
            payload = json.loads(self._sequence_path.read_text(encoding="utf-8"))
            sequence = max(0, int(payload.get("next_sequence") or 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            sequence = 0
        atomic_text_replace(
            self._sequence_path,
            json.dumps({"next_sequence": sequence + 1}),
        )
        return sequence

    def _lead_for(self, sequence: int) -> int:
        span = self.lead_max - self.lead_min + 1
        return self.lead_min + (sequence % span)

    def _persist_locked(self) -> None:
        payload = {
            "version": QUEUE_STATE_VERSION,
            "songs_remaining": self.songs_remaining,
            "missed_boundary_count": self.missed_boundary_count,
            "stale_boundary_count": self.stale_boundary_count,
            "last_boundary_failure": self.last_boundary_failure,
            "prepared_boundary_count": self.prepared_boundary_count,
            "items": [
                {
                    "file": item.path.name,
                    "sequence": item.sequence,
                    "prepared": item.sequence in self.prepared_sequences,
                    "context_key": self.prepared_contexts.get(item.sequence, ""),
                }
                for item in self.ready
            ],
        }
        try:
            atomic_text_replace(self._queue_path, json.dumps(payload, indent=2))
        except OSError:
            # No-dead-air: persistence failure must never kill the host thread.
            # Next _persist_locked will retry; speech pipeline stays alive.
            pass

    def _restore(self) -> None:
        restored: deque[QueuedHostClip] = deque()
        songs_remaining: int | None = None
        prepared_sequences: set[int] = set()
        prepared_contexts: dict[int, str] = {}
        try:
            payload = json.loads(self._queue_path.read_text(encoding="utf-8"))
            version = int(payload.get("version") or 0)
            if version != QUEUE_STATE_VERSION:
                raise ValueError("unsupported host queue state")
            self.missed_boundary_count = max(0, int(payload.get("missed_boundary_count") or 0))
            self.stale_boundary_count = max(0, int(payload.get("stale_boundary_count") or 0))
            self.last_boundary_failure = dict(payload.get("last_boundary_failure") or {})
            self.prepared_boundary_count = max(0, int(payload.get("prepared_boundary_count") or 0))
            for raw in payload.get("items") or []:
                candidate = self.root / Path(str(raw.get("file") or "")).name
                sequence = int(raw.get("sequence"))
                if candidate.suffix.casefold() == ".wav" and candidate.name.startswith("host-"):
                    restored.append(QueuedHostClip(candidate, sequence))
                    try:
                        clip_meta = json.loads(
                            candidate.with_suffix(".json").read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        clip_meta = {}
                    context_key = str(raw.get("context_key") or "").strip().casefold()
                    line = valid_line(clip_meta.get("line"), self.language)
                    recorded_context_key = self._context_key(
                        design_id=str(clip_meta.get("voice_design") or ""),
                        title=str(clip_meta.get("previous_title") or ""),
                        artist=str(clip_meta.get("previous_artist") or ""),
                        next_title=str(clip_meta.get("next_title") or ""),
                        next_artist=str(clip_meta.get("next_artist") or ""),
                        well_wish=bool(clip_meta.get("well_wish")),
                        sequence=sequence,
                    )
                    is_prepared = (
                        valid_mono_wav(candidate)
                        and raw.get("prepared") is True
                        and clip_meta.get("version") == 2
                        and clip_meta.get("audio_quality_version") == AUDIO_QUALITY_VERSION
                        and clip_meta.get("language") == self.language
                        and clip_meta.get("text_model") == self.model
                        and clip_meta.get("tts_engine") == "kokoro"
                        and clip_meta.get("tts_model") == TTS_MODEL_NAME
                        and bool(clip_meta.get("well_wish"))
                        == _well_wish_due(sequence)
                        and line is not None
                        and context_key == recorded_context_key
                        and completed_host_wav_rejection(
                            candidate, normalize_tts_text(line)
                        )
                        is None
                    )
                    if is_prepared and re.fullmatch(r"[0-9a-f]{64}", context_key):
                        prepared_sequences.add(sequence)
                        prepared_contexts[sequence] = context_key
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            restored.clear()
        while len(restored) > self.queue_target:
            restored.pop().path.unlink(missing_ok=True)
        referenced = {item.path.resolve() for item in restored}
        for stale in self.root.glob("host-*.wav"):
            if stale.resolve() not in referenced:
                stale.unlink(missing_ok=True)
        with self.lock:
            self.ready = restored
            restored_sequences = {item.sequence for item in restored}
            self.prepared_sequences = prepared_sequences & restored_sequences
            self.prepared_contexts = {
                sequence: context_key
                for sequence, context_key in prepared_contexts.items()
                if sequence in restored_sequences
            }
            self.preparing_sequences.clear()
            self.preparing_contexts.clear()
            # Re-start the lead window from the first restored slot. The old
            # countdown belongs to the interrupted program and may announce
            # its tracks immediately after a new program starts.
            self.songs_remaining = (
                self._lead_for(restored[0].sequence) if restored else None
            )
            self._persist_locked()

    def start(self, *, invalidate_restored_contexts: bool = False) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "cache").mkdir(parents=True, exist_ok=True)
        for stale in self.root.glob("*.part.wav"):
            stale.unlink(missing_ok=True)
        for stale in (*self.root.glob("*.pending.json"), *self.root.glob("*.pending.tmp")):
            stale.unlink(missing_ok=True)
        self._restore()
        if invalidate_restored_contexts:
            # Restore the durable slots and counters before invalidation writes
            # them back. Invalidating an empty constructor wipes the checkpoint.
            self.invalidate_restored_song_contexts()
        with self.lock:
            self._fill_slots_locked()
        self.thread.start()

    @property
    def _current_design(self) -> str:
        return (
            self.night_voice_design
            if current_daypart() == "night"
            else self.voice_design
        )

    def _pool_dir(self, design_id: str) -> Path:
        return self.root / "pool" / design_id

    def _pool_sidecar(self, audio_path: Path) -> Path:
        return audio_path.with_suffix(".json")

    def _valid_pool_clip(self, audio_path: Path) -> bool:
        if not valid_mono_wav(audio_path):
            return False
        try:
            metadata = json.loads(
                self._pool_sidecar(audio_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        return bool(
            metadata.get("tts_engine") == "kokoro"
            and metadata.get("tts_model") == TTS_MODEL_NAME
            and valid_line(metadata.get("line"), self.language)
        )

    def _pool_ready(self, design_id: str) -> list[Path]:
        directory = self._pool_dir(design_id)
        if not directory.is_dir():
            return []
        return sorted(
            path
            for path in directory.glob("liner-*.wav")
            if self._valid_pool_clip(path)
        )

    def liner_pool_snapshot(self) -> dict[str, object]:
        design = self._current_design
        ready = len(self._pool_ready(design))
        return {"design": design, "ready": ready, "target": LINER_POOL_TARGET}

    def _fill_pool_once(self) -> bool:
        """Render at most one missing evergreen liner; True when work was done."""
        design = self._current_design
        directory = self._pool_dir(design)
        directory.mkdir(parents=True, exist_ok=True)
        existing = {
            int(path.stem.split("-")[1])
            for path in directory.glob("liner-*.wav")
            if self._valid_pool_clip(path) and path.stem.split("-")[1].isdigit()
        }
        lines = LINERS.get(self.language) or ()
        index = next((i for i in range(len(lines)) if i not in existing), None)
        if index is None or len(existing) >= LINER_POOL_TARGET:
            return False
        text = valid_line(lines[index], self.language)
        if not text:
            return False
        target = directory / f"liner-{index:03d}.wav"
        temporary = target.with_suffix(".part.wav")
        try:
            _synthesize(
                text,
                design,
                temporary,
                language=self.language,
                qwen_url=self.qwen_url,
                synthesis_seed=self.seed + index,
                priority="queue",
            )
            temporary.replace(target)
            atomic_text_replace(
                self._pool_sidecar(target),
                json.dumps(
                    {
                        "version": 1,
                        "language": self.language,
                        "line": text,
                        "text_model": QWEN_TEXT_MODEL,
                        "tts_engine": "kokoro",
                        "tts_model": TTS_MODEL_NAME,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            return True
        finally:
            if temporary.exists():
                temporary.unlink()

    def _fill_pool_forever(self) -> None:
        while not self.stop.wait(15):
            try:
                self._fill_pool_once()
            except Exception:
                continue

    def next_liner_clip(self) -> Path | None:
        """Legacy compatibility hook; generic liners are never broadcast."""
        # A boundary must be tied to the actual preceding and following songs.
        # Keep this method for old callers, but fail closed instead of allowing
        # a stale evergreen clip (for example “another song is on the way”) to
        # enter the playout path.
        return None

    def _fill_slots_locked(self) -> None:
        while len(self.ready) < self.queue_target:
            sequence = self._next_sequence()
            target = self.root / f"host-{sequence:012d}-{time.time_ns()}.wav"
            self.ready.append(QueuedHostClip(target, sequence))
            if self.songs_remaining is None:
                self.songs_remaining = self._lead_for(sequence)
        self._persist_locked()

    def _produce(self) -> None:
        """Fill durable schedule slots; only contextual Kokoro audio may fill them."""
        while not self.stop.is_set():
            try:
                with self.lock:
                    self._fill_slots_locked()
            except Exception:
                # Keep scheduler alive even during transient IO contention.
                pass
            self.stop.wait(1)

    def _context_key(
        self,
        *,
        design_id: str,
        title: str,
        artist: str,
        next_title: str,
        next_artist: str,
        well_wish: bool,
        sequence: int = 0,
    ) -> str:
        identity = json.dumps(
            {
                "version": 4,
                "copy_version": ANNOUNCEMENT_COPY_VERSION,
                "announcement_sequence": sequence,
                "voice_design_version": TTS_VOICE_VERSION,
                "audio_quality_version": AUDIO_QUALITY_VERSION,
                "language": self.language,
                "text_model": self.model,
                "tts_engine": "kokoro",
                "tts_model": TTS_MODEL_NAME,
                "voice_design": design_id,
                "title": title,
                "artist": artist,
                "next_title": next_title,
                "next_artist": next_artist,
                "well_wish": bool(well_wish),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _cache_paths(self, context_key: str) -> tuple[Path, Path]:
        cache_root = self.root / "cache"
        return cache_root / f"{context_key}.wav", cache_root / f"{context_key}.json"

    def _restore_cached_context(
        self, context_key: str, target: Path
    ) -> str | None:
        audio_path, metadata_path = self._cache_paths(context_key)
        if not metadata_path.is_file():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                metadata.get("context_key") != context_key
                or metadata.get("audio_quality_version") != AUDIO_QUALITY_VERSION
                or metadata.get("text_model") != QWEN_TEXT_MODEL
                or metadata.get("tts_engine") != "kokoro"
                or metadata.get("tts_model") != TTS_MODEL_NAME
            ):
                return None
            line = valid_line(metadata.get("line"), self.language)
            if not line:
                return None
            if completed_host_wav_rejection(audio_path, normalize_tts_text(line)) is not None:
                return None
            temporary = target.with_suffix(".cache.part.wav")
            shutil.copy2(audio_path, temporary)
            temporary.replace(target)
            return line
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _store_cached_context(
        self, context_key: str, source: Path, line: str, copy_source: str = ""
    ) -> None:
        rejection = completed_host_wav_rejection(source, normalize_tts_text(line))
        if rejection is not None:
            raise RuntimeError(rejection)
        audio_path, metadata_path = self._cache_paths(context_key)
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_temporary = audio_path.with_suffix(".part.wav")
        shutil.copy2(source, audio_temporary)
        audio_temporary.replace(audio_path)
        atomic_text_replace(
            metadata_path,
            json.dumps(
                {
                    "version": 2,
                    "context_key": context_key,
                    "audio_quality_version": AUDIO_QUALITY_VERSION,
                    "language": self.language,
                    "line": line,
                    "copy_source": copy_source,
                    "text_model": QWEN_TEXT_MODEL,
                    "tts_engine": "kokoro",
                    "tts_model": TTS_MODEL_NAME,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    def _prepared_ahead_locked(self) -> int:
        count = 0
        for item in self.ready:
            if item.sequence not in self.prepared_sequences:
                break
            count += 1
        return count

    def wait_until_prepared(self, minimum: int | None = None) -> bool:
        """Wait through warm-up until the next contiguous announcement horizon is ready."""
        required = max(1, min(int(minimum or self.startup_min), self.queue_target))
        while not self.stop.is_set():
            with self.lock:
                if self._prepared_ahead_locked() >= required:
                    return True
            self.stop.wait(1)
        return False

    def scheduled_song_offsets(self) -> list[int]:
        """Map each durable host slot to its future zero-based song boundary."""
        with self.lock:
            if not self.ready:
                return []
            items = list(self.ready)
            cumulative = max(
                1,
                int(
                    self.songs_remaining
                    if self.songs_remaining is not None
                    else self._lead_for(items[0].sequence)
                ),
            )
            offsets = [cumulative - 1]
            for item in items[1:]:
                cumulative += self._lead_for(item.sequence)
                offsets.append(cumulative - 1)
            return offsets

    def invalidate_restored_song_contexts(self) -> None:
        """Restored clips need an exact match against the new live song horizon."""
        with self.lock:
            self.prepared_sequences.clear()
            self.prepared_contexts.clear()
            self._persist_locked()

    def prepare_for_song(
        self,
        *,
        title: str,
        artist: str,
        next_title: str = "",
        next_artist: str = "",
        offset: int = 0,
        expected_sequence: int | None = None,
    ) -> bool:
        """Prepare a song-aware link as soon as its scheduled horizon is known."""
        title = normalize_spoken_metadata(title)
        artist = normalize_spoken_metadata(artist)
        next_title = normalize_spoken_metadata(next_title)
        next_artist = normalize_spoken_metadata(next_artist)
        if not safe_spoken_metadata(title, artist, next_title, next_artist):
            with self.lock:
                self.last_error = "unsafe or incomplete song metadata was not announced"
            return False
        daypart = current_daypart()
        design_id = self.night_voice_design if daypart == "night" else self.voice_design
        with self.lock:
            if not self.ready or self.songs_remaining is None or offset < 0 or offset >= len(self.ready):
                return False
            item = list(self.ready)[offset]
            if expected_sequence is not None and item.sequence != expected_sequence:
                return False
            context_key = self._context_key(
                design_id=design_id,
                title=title,
                artist=artist,
                next_title=next_title,
                next_artist=next_artist,
                well_wish=_well_wish_due(item.sequence),
                sequence=item.sequence,
            )
            if (
                item.sequence in self.prepared_sequences
                and self.prepared_contexts.get(item.sequence) == context_key
            ):
                return True
            if item.sequence in self.preparing_sequences:
                return self.preparing_contexts.get(item.sequence) == context_key
            if item.sequence in self.prepared_sequences:
                self.prepared_sequences.discard(item.sequence)
                self.prepared_contexts.pop(item.sequence, None)
                item.path.unlink(missing_ok=True)
            self.preparing_sequences.add(item.sequence)
            self.preparing_contexts[item.sequence] = context_key

        def prepare() -> None:
            contextual = item.path.with_name(f"{item.path.stem}.context.wav")
            pending = item.path.with_suffix(".pending.json")
            try:
                generation_details: dict[str, str] = {}
                text = self._restore_cached_context(context_key, item.path)
                if text is None:
                    with self.copy_lock:
                        text = generate_line(
                            self.language,
                            self.ollama_url,
                            self.model,
                            item.sequence,
                            self.seed,
                            previous_title=title,
                            previous_artist=artist,
                            next_title=next_title,
                            next_artist=next_artist,
                            daypart=daypart,
                            recent_scripts=tuple(self.recent_scripts),
                            generation_details=generation_details,
                        )
                        self.recent_scripts.append(announcement_script(
                            text, previous_title=title, previous_artist=artist,
                            next_title=next_title, next_artist=next_artist,
                            station=SPOKEN_STATION_NAME,
                        ))
                        atomic_text_replace(self.root / "announcement-history.json", json.dumps(
                            {"copy_version": ANNOUNCEMENT_COPY_VERSION, "scripts": list(self.recent_scripts)},
                            ensure_ascii=False, indent=2,
                        ))
                    atomic_text_replace(
                        pending,
                        json.dumps(
                            {
                                "context_key": context_key,
                                "language": self.language,
                                "line": text,
                                "text_model": QWEN_TEXT_MODEL,
                                "tts_engine": "kokoro",
                                "tts_model": TTS_MODEL_NAME,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                    # One small CPU-bound speech model is shared by both stations.
                    # Serialize calls here so stale song contexts cannot pile up
                    # behind the model and starve the on-air horizon.
                    with MODEL_GENERATION_LOCK:
                        _synthesize(
                            text,
                            design_id,
                            contextual,
                            language=self.language,
                            qwen_url=self.qwen_url,
                            synthesis_seed=self.seed + (item.sequence * 7_919),
                            priority="context",
                        )
                    self._store_cached_context(context_key, contextual, text, generation_details.get("copy_source", ""))
                else:
                    _, cached_metadata = self._cache_paths(context_key)
                    generation_details["copy_source"] = json.loads(cached_metadata.read_text(encoding="utf-8")).get("copy_source", "")
                atomic_text_replace(
                    item.path.with_suffix(".json"),
                    json.dumps(
                        {
                            "version": 2,
                            "copy_version": ANNOUNCEMENT_COPY_VERSION,
                            "copy_source": generation_details.get("copy_source", ""),
                            "audio_quality_version": AUDIO_QUALITY_VERSION,
                            "language": self.language,
                            "line": text,
                            "well_wish": _well_wish_due(item.sequence),
                            "voice_design": design_id,
                            "previous_title": title,
                            "previous_artist": artist,
                            "next_title": next_title,
                            "next_artist": next_artist,
                            "text_model": self.model,
                            "tts_engine": "kokoro",
                            "tts_model": TTS_MODEL_NAME,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                with self.lock:
                    if (
                        any(candidate.sequence == item.sequence for candidate in self.ready)
                        and self.preparing_contexts.get(item.sequence) == context_key
                    ):
                        if contextual.exists():
                            contextual.replace(item.path)
                        self.prepared_sequences.add(item.sequence)
                        self.prepared_contexts[item.sequence] = context_key
                        self.last_prepared_line = text
                        self._persist_locked()
                    else:
                        contextual.unlink(missing_ok=True)
                    self.last_error = ""
            except Exception as exc:
                contextual.unlink(missing_ok=True)
                with self.lock:
                    self.last_error = str(exc)[:240]
            finally:
                pending.unlink(missing_ok=True)
                with self.lock:
                    self.preparing_sequences.discard(item.sequence)
                    self.preparing_contexts.pop(item.sequence, None)
                    self.context_threads.pop(item.sequence, None)

        context_thread = threading.Thread(
            target=prepare,
            name=f"dynamic-host-context-{self.language}-{item.sequence}",
            daemon=True,
        )
        with self.lock:
            self.context_threads[item.sequence] = context_thread
        context_thread.start()
        return True

    def prepared_clips(self, limit: int = 2) -> list[Path]:
        """Return only validated slot audio for background PCM preparation."""
        with self.lock:
            return [item.path for item in self.ready
                    if item.sequence in self.prepared_sequences][:limit]

    def after_song(self, *, skip_unprepared: bool = False,
                   expected_song_context: dict[str, str] | None = None) -> Path | None:
        with self.lock:
            if not self.ready or self.songs_remaining is None:
                return None
            self.songs_remaining -= 1
            if self.songs_remaining > 0:
                self._persist_locked()
                return None
            if expected_song_context is not None and self.ready[0].sequence in self.prepared_sequences:
                try:
                    metadata = json.loads(self.ready[0].path.with_suffix(".json").read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    metadata = {}
                if metadata.get("copy_version") != ANNOUNCEMENT_COPY_VERSION or any(
                    metadata.get(key) != normalize_spoken_metadata(value)
                    for key, value in expected_song_context.items()
                ):
                    self.prepared_sequences.discard(self.ready[0].sequence)
                    self.prepared_contexts.pop(self.ready[0].sequence, None)
                    self.last_error = "stale announcement did not match the actual adjacent songs"
                    self.stale_boundary_count += 1
                    self.last_boundary_failure = {
                        "observed_at": datetime.now().astimezone().isoformat(),
                        "sequence": self.ready[0].sequence, "reason": "stale_context",
                        "expected": {key: normalize_spoken_metadata(value)
                                     for key, value in expected_song_context.items()},
                        "actual": {key: metadata.get(key) for key in expected_song_context},
                    }
            # If the exact title/artist context is still rendering, do not
            # substitute generic speech. Keep the music timeline continuous
            # and retry this boundary only after a validated contextual WAV
            # has been atomically published into the queue.
            if self.ready[0].sequence not in self.prepared_sequences:
                self.missed_boundary_count += 1
                if skip_unprepared:
                    expired = self.ready.popleft()
                    self.preparing_sequences.discard(expired.sequence)
                    self.preparing_contexts.pop(expired.sequence, None)
                    self.prepared_contexts.pop(expired.sequence, None)
                    expired.path.unlink(missing_ok=True)
                    self.songs_remaining = (
                        self._lead_for(self.ready[0].sequence) if self.ready else None
                    )
                else:
                    self.songs_remaining = 1
                self._persist_locked()
                # Never air a generic “another song is on the way” clip in
                # place of a song-grounded announcement. Music continues on the
                # same PCM timeline while the exact context is retried.
                return None
            result = self.ready.popleft()
            self.prepared_boundary_count += 1
            self.prepared_sequences.discard(result.sequence)
            self.prepared_contexts.pop(result.sequence, None)
            self.preparing_sequences.discard(result.sequence)
            self.preparing_contexts.pop(result.sequence, None)
            self.songs_remaining = (
                self._lead_for(self.ready[0].sequence) if self.ready else None
            )
            self._persist_locked()
            return result.path

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "mode": "dynamic-ephemeral",
                "scheduling": "deterministic",
                "seed": self.seed,
                "queue_depth": len(self.ready),
                "queue_target": self.queue_target,
                "lead_min": self.lead_min,
                "lead_max": self.lead_max,
                "prepared_count": len(self.prepared_sequences),
                "prepared_ahead_count": self._prepared_ahead_locked(),
                "missed_boundary_count": self.missed_boundary_count,
                "stale_boundary_count": self.stale_boundary_count,
                "last_boundary_failure": dict(self.last_boundary_failure),
                "prepared_boundary_count": self.prepared_boundary_count,
                "startup_min": self.startup_min,
                "rolling_prepare": self.rolling_prepare,
                "preparing_count": len(self.preparing_sequences),
                "songs_until_air": self.songs_remaining,
                "scheduled_song_offsets": self.scheduled_song_offsets(),
                "scheduled_sequences": [item.sequence for item in self.ready],
                "last_error": self.last_error,
                "tts_engine": "kokoro",
                "tts_model": TTS_MODEL_NAME,
                "text_model": QWEN_TEXT_MODEL,
                "copy_version": ANNOUNCEMENT_COPY_VERSION,
                "recent_script_count": len(self.recent_scripts),
                "tts_url": self.qwen_url,
                "fallback_tts": False,
                "audio_quality_gate": AUDIO_QUALITY_VERSION,
                "quality_attempts": TTS_QUALITY_ATTEMPTS,
                "voice_design": (
                    self.night_voice_design
                    if current_daypart() == "night"
                    else self.voice_design
                ),
                "delivery": current_daypart(),
                "spoken_station_name": SPOKEN_STATION_NAME,
                "song_context_ready": bool(
                    self.ready and self.ready[0].sequence in self.prepared_sequences
                ),
                "liner_pool": self.liner_pool_snapshot(),
            }

    def played(self, path: Path) -> None:
        # Evergreen liners are reused across boundaries; only ephemeral slot
        # audio directly inside the queue root may be deleted after air.
        if path.parent == self.root and path.name.startswith("host-"):
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)

    def announcement_line(self, path: Path) -> str | None:
        """Return the grounded text associated with the exact queued WAV."""
        if path.parent != self.root or not path.name.startswith("host-"):
            return None
        try:
            metadata = json.loads(
                path.with_suffix(".json").read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            metadata.get("tts_engine") != "kokoro"
            or metadata.get("tts_model") != TTS_MODEL_NAME
        ):
            return None
        return valid_line(metadata.get("line"), self.language)

    def announcement_metadata(self, path: Path) -> dict[str, object]:
        """Return public-safe synthesis metadata for the exact queued WAV."""
        if path.parent != self.root or not path.name.startswith("host-"):
            return {}
        try:
            metadata = json.loads(
                path.with_suffix(".json").read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        if (
            metadata.get("tts_engine") != "kokoro"
            or metadata.get("tts_model") != TTS_MODEL_NAME
        ):
            return {}
        match = re.match(r"host-(\d+)-", path.name)
        sequence = int(match.group(1)) if match else 0
        return {
            "text_model": None if metadata.get("copy_source") == "grounded_variety_fallback" else str(metadata.get("text_model") or QWEN_TEXT_MODEL),
            "tts_engine": "kokoro",
            "tts_model": TTS_MODEL_NAME,
            "voice_design": str(metadata.get("voice_design") or "") or None,
            "well_wish": bool(metadata.get("well_wish", _well_wish_due(sequence))),
        }

    def close(self) -> None:
        self.thread.join(timeout=5)
        if self.pool_thread is not None:
            self.pool_thread.join(timeout=5)
        with self.lock:
            context_threads = list(self.context_threads.values())
        for context_thread in context_threads:
            context_thread.join(timeout=1)
        with self.lock:
            self._persist_locked()
