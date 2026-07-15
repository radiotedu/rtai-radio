from __future__ import annotations

from dataclasses import dataclass


RESEARCH_GENRES = frozenset({"jazz", "classical"})

POP_TEMPLATES = {
    "en": {
        "morning": (
            "Good morning from Radio TED U—here's {title} by {artist}.",
            "Have a bright morning with Radio TED U. Here's {title} by {artist}.",
        ),
        "daytime": (
            "You're with Radio TED U. Have a great afternoon—here's {title} by {artist}.",
            "Stay with Radio TED U for more music. Here's {title} by {artist}.",
        ),
        "night": (
            "You're listening to Radio TED U tonight. Here's {title} by {artist}.",
            "Stay with Radio TED U—here's {title} by {artist}.",
        ),
        "weekend": (
            "Enjoy your weekend with Radio TED U—here's {title} by {artist}.",
            "Radio TED U keeps your weekend moving with {title} by {artist}.",
        ),
    },
    "fr": {
        "morning": (
            "Bonjour, vous écoutez Radio TED U—voici {title} de {artist}.",
            "Passez une belle matinée avec Radio TED U. Voici {title} de {artist}.",
        ),
        "daytime": (
            "Vous écoutez Radio TED U. Passez une excellente journée—voici {title} de {artist}.",
            "Restez avec Radio TED U pour plus de musique. Voici {title} de {artist}.",
        ),
        "night": (
            "Vous passez la soirée avec Radio TED U—voici {title} de {artist}.",
            "Restez avec Radio TED U—voici {title} de {artist}.",
        ),
        "weekend": (
            "Bon week-end avec Radio TED U—voici {title} de {artist}.",
            "Radio TED U accompagne votre week-end avec {title} de {artist}.",
        ),
    },
}


@dataclass(frozen=True, slots=True)
class PopLiner:
    template_id: str
    text: str


def normalize_genre(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def research_allowed(genre: object) -> bool:
    return normalize_genre(genre) in RESEARCH_GENRES


def build_pop_liner(
    language: str,
    daypart: str,
    title: str,
    artist: str,
    recent_template_ids=(),
) -> PopLiner:
    templates = POP_TEMPLATES[language][daypart]
    recent = set(recent_template_ids)
    index = next(
        (index for index in range(len(templates)) if f"{language}:{daypart}:{index}" not in recent),
        0,
    )
    template_id = f"{language}:{daypart}:{index}"
    return PopLiner(template_id, templates[index].format(title=title, artist=artist))

