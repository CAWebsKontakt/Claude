"""Fachliche Zustände. In der Datenbank als VARCHAR gespeichert (einfachere Migrationen als native ENUMs)."""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"  # alles inkl. Benutzerverwaltung
    EDITOR = "editor"  # Streamer, Rechte, Highlights, Freigaben
    VIEWER = "viewer"  # nur lesen


ROLE_LABELS = {
    UserRole.ADMIN: "Administrator",
    UserRole.EDITOR: "Redakteur",
    UserRole.VIEWER: "Betrachter",
}


class StreamerMode(StrEnum):
    PUBLIC = "public"  # Modus A: nur öffentliche Metadaten, Links, Empfehlungen
    AUTHORIZED = "authorized"  # Modus B: OAuth + dokumentierte Erlaubnis


MODE_LABELS = {
    StreamerMode.PUBLIC: "Modus A – Öffentlich",
    StreamerMode.AUTHORIZED: "Modus B – Autorisiert",
}


class RightsStatus(StrEnum):
    PUBLIC_ONLY = "public_only"
    MISSING_PERMISSION = "missing_permission"
    MISSING_OAUTH = "missing_oauth"
    READY_PROCESSING = "ready_processing"
    READY_PUBLISHING = "ready_publishing"


class GrantorRole(StrEnum):
    BROADCASTER = "broadcaster"
    EDITOR = "editor"
    MANAGEMENT = "management"
    OTHER = "other"


GRANTOR_LABELS = {
    GrantorRole.BROADCASTER: "Streamer selbst",
    GrantorRole.EDITOR: "Berechtigter Editor",
    GrantorRole.MANAGEMENT: "Management/Agentur",
    GrantorRole.OTHER: "Sonstige",
}


class OAuthProvider(StrEnum):
    TWITCH = "twitch"
    TIKTOK = "tiktok"


class ActivityLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ActivityCategory(StrEnum):
    AUTH = "auth"
    USER = "user"
    STREAMER = "streamer"
    PERMISSION = "permission"
    TWITCH = "twitch"
    PIPELINE = "pipeline"
    RENDER = "render"
    TIKTOK = "tiktok"
    SYSTEM = "system"


class StreamStatus(StrEnum):
    LIVE = "live"
    ENDED = "ended"
    PROCESSING = "processing"
    PROCESSED = "processed"
    SKIPPED = "skipped"


class ClipStatus(StrEnum):
    NEW = "new"
    RECOMMENDED = "recommended"
    DISMISSED = "dismissed"


class SourceType(StrEnum):
    VOD = "vod"
    CLIP = "clip"
    SAMPLE = "sample"


class HighlightStatus(StrEnum):
    CANDIDATE = "candidate"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    RENDERING = "rendering"
    RENDERED = "rendered"
    QUEUED = "queued"
    PUBLISHED = "published"
    FAILED = "failed"


HIGHLIGHT_STATUS_LABELS = {
    HighlightStatus.CANDIDATE: "Kandidat",
    HighlightStatus.REJECTED: "Verworfen",
    HighlightStatus.DUPLICATE: "Duplikat",
    HighlightStatus.RENDERING: "Wird geschnitten",
    HighlightStatus.RENDERED: "Geschnitten",
    HighlightStatus.QUEUED: "In Warteschlange",
    HighlightStatus.PUBLISHED: "Veröffentlicht",
    HighlightStatus.FAILED: "Fehler",
}


class PublishMethod(StrEnum):
    DIRECT_POST = "direct_post"  # nur mit auditierter App
    INBOX_UPLOAD = "inbox_upload"  # Entwurf in die TikTok-Inbox, Nutzer veröffentlicht in der App
    MANUAL_EXPORT = "manual_export"  # Datei + Texte zum manuellen Hochladen


PUBLISH_METHOD_LABELS = {
    PublishMethod.DIRECT_POST: "Direct Post",
    PublishMethod.INBOX_UPLOAD: "Upload in TikTok-Inbox",
    PublishMethod.MANUAL_EXPORT: "Manueller Export",
}


class PublishStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    SENT_TO_INBOX = "sent_to_inbox"
    PUBLISHED = "published"
    EXPORTED = "exported"
    FAILED = "failed"
    CANCELLED = "cancelled"


PUBLISH_STATUS_LABELS = {
    PublishStatus.PENDING_REVIEW: "Wartet auf Freigabe",
    PublishStatus.APPROVED: "Freigegeben",
    PublishStatus.UPLOADING: "Wird hochgeladen",
    PublishStatus.PROCESSING: "TikTok verarbeitet",
    PublishStatus.SENT_TO_INBOX: "In TikTok-Inbox",
    PublishStatus.PUBLISHED: "Veröffentlicht",
    PublishStatus.EXPORTED: "Exportiert",
    PublishStatus.FAILED: "Fehlgeschlagen",
    PublishStatus.CANCELLED: "Abgebrochen",
}

QUEUE_STATUSES = (
    PublishStatus.PENDING_REVIEW,
    PublishStatus.APPROVED,
    PublishStatus.UPLOADING,
    PublishStatus.PROCESSING,
    PublishStatus.FAILED,
)
DONE_STATUSES = (PublishStatus.PUBLISHED, PublishStatus.SENT_TO_INBOX, PublishStatus.EXPORTED)
# Videos in diesen Zuständen dürfen von der Speicherbereinigung nicht gelöscht werden.
PROTECTED_PUBLISH_STATUSES = (
    PublishStatus.PENDING_REVIEW,
    PublishStatus.APPROVED,
    PublishStatus.UPLOADING,
    PublishStatus.PROCESSING,
)


class TwitchAuthRole(StrEnum):
    BROADCASTER = "broadcaster"
    EDITOR = "editor"


class ClipRequestSource(StrEnum):
    VIEWER_CLIP = "viewer_clip"  # vorhandener Zuschauer-Clip des Streams
    VOD_WINDOW = "vod_window"  # neuer Clip aus dem VOD (Signale, ab Phase 4)
    MANUAL = "manual"  # im Dashboard angefordert
    SAMPLE = "sample"  # Beispieldatei im Testmodus


CLIP_SOURCE_LABELS = {
    ClipRequestSource.VIEWER_CLIP: "Zuschauer-Clip",
    ClipRequestSource.VOD_WINDOW: "Aus VOD (automatisch)",
    ClipRequestSource.MANUAL: "Aus VOD (manuell)",
    ClipRequestSource.SAMPLE: "Beispieldatei",
}


class ClipRequestStatus(StrEnum):
    PLANNED = "planned"
    CREATING = "creating"
    WAITING = "waiting"  # Clip erstellt, Twitch verarbeitet noch
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"  # Rechte fehlen o. Ä.
    DUPLICATE = "duplicate"
    FAILED = "failed"


CLIP_STATUS_LABELS = {
    ClipRequestStatus.PLANNED: "Geplant",
    ClipRequestStatus.CREATING: "Wird erstellt",
    ClipRequestStatus.WAITING: "Twitch verarbeitet",
    ClipRequestStatus.DOWNLOADING: "Wird geladen",
    ClipRequestStatus.DOWNLOADED: "Bereit für Analyse",
    ClipRequestStatus.SKIPPED: "Übersprungen",
    ClipRequestStatus.DUPLICATE: "Duplikat",
    ClipRequestStatus.FAILED: "Fehlgeschlagen",
}
ACTIVE_CLIP_STATUSES = (
    ClipRequestStatus.PLANNED,
    ClipRequestStatus.CREATING,
    ClipRequestStatus.WAITING,
    ClipRequestStatus.DOWNLOADING,
)
