"""Vorlagen für die Anfrage an Streamer (Zustimmungslink).

Kurz, ehrlich und mit klarem Nutzen für den Streamer. Twitch-Flüsternachrichten sind auf 500 Zeichen begrenzt.
"""

from __future__ import annotations

from dataclasses import dataclass

WHISPER_LIMIT = 500


@dataclass(frozen=True)
class Message:
    key: str
    title: str
    text: str
    hint: str = ""


def _tiktok(targets: list[str]) -> str:
    return " / ".join(targets) if targets else "meinem TikTok-Kanal"


def build_messages(
    *, channel: str, display_name: str, link: str, publishing: bool, targets: list[str], sender: str = ""
) -> list[Message]:
    name = display_name or channel
    where = _tiktok(targets)
    sign = f"\n\nViele Grüße\n{sender}" if sender else "\n\nViele Grüße"
    goal = (
        f"kurze Hochformat-Clips deiner besten Momente für TikTok ({where}) zu machen – "
        f"immer mit Hinweis auf twitch.tv/{channel}, damit neue Zuschauer zu dir finden"
        if publishing
        else "kurze Hochformat-Clips deiner besten Momente zu erstellen"
    )

    whisper = (
        f"Hey {name}! Darf ich aus deinen Streams kostenlos TikTok-Clips machen? "
        "Mit Verweis auf deinen Kanal, ohne fremde Musik, jedes Video vorher geprüft. "
        f"Zustimmung mit einem Klick (Twitch-Login, jederzeit widerrufbar): {link}"
        if publishing
        else f"Hey {name}! Darf ich aus deinen Streams Hochformat-Clips erstellen? "
        f"Zustimmung mit einem Klick (Twitch-Login, jederzeit widerrufbar): {link}"
    )

    long_de = (
        f"Hallo {name},\n\n"
        f"ich würde gern {goal}.\n\n"
        "So läuft es ab:\n"
        "• Eine KI findet nach deinem Stream die stärksten Momente (Reaktionen, Lacher, Clutches).\n"
        "• Daraus entstehen 20–45 Sekunden lange Clips im Hochformat mit gut lesbaren Untertiteln.\n"
        + (
            "• Jedes Video sehe ich mir vorher an und gebe es von Hand frei – nichts geht automatisch raus.\n"
            if publishing
            else ""
        )
        + "• Es wird keine fremde Musik hinzugefügt, dein Originalton bleibt.\n"
        "• Es kostet dich nichts, und du kannst jederzeit widerrufen "
        "(Twitch → Einstellungen → Verbindungen).\n\n"
        "Wenn du einverstanden bist, öffne bitte diesen Link und melde dich mit deinem Twitch-Konto an – "
        "dort siehst du genau, was du erlaubst, und kannst einzelne Punkte abwählen:\n"
        f"{link}\n\n"
        "Bei Fragen melde dich gern jederzeit." + sign
    )

    short_en = (
        f"Hey {name}! Could I turn your best stream moments into short vertical clips"
        + (f" for TikTok ({where}), always crediting twitch.tv/{channel}" if publishing else "")
        + "? Free for you, no added music, every video reviewed by hand. One-click consent via Twitch login "
        f"(revoke anytime): {link}"
    )

    # Nie den Link abschneiden: bei Überlänge auf die Kurzfassung ausweichen
    if len(whisper) > WHISPER_LIMIT:
        whisper = f"Hey {name}! Darf ich aus deinen Streams Clips machen? Zustimmung per Klick: {link}"
    if len(short_en) > WHISPER_LIMIT:
        short_en = f"Hey {name}! May I make clips from your streams? One-click consent: {link}"

    follow_up = (
        f"Hi {name}, kurze Erinnerung an meine Frage wegen der Clips – kein Stress, falls du nicht möchtest. "
        f"Der Link ist noch einige Tage gültig: {link}"
    )

    return [
        Message(
            "whisper",
            "Twitch-Flüsternachricht (kurz)",
            whisper,
            "Twitch → Kanal → Flüstern. Max. 500 Zeichen; neue Konten dürfen evtl. keine Links flüstern.",
        ),
        Message(
            "long",
            "Discord / E-Mail (ausführlich)",
            long_de,
            "Kontakt steht oft im Twitch-Profil unter „Über mich“ (Business-E-Mail, Discord-Server).",
        ),
        Message("english", "Englisch (kurz)", short_en, "Für internationale Streamer."),
        Message("follow_up", "Erinnerung (nach 3–5 Tagen, einmal)", follow_up, "Nur einmal nachfragen."),
    ]
