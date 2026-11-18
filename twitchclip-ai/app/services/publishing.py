"""TikTok-Warteschlange, Freigabe und Veröffentlichung (Phase 6).

Grundsätze:
- Nichts wird ohne ausdrückliche, einzelne Freigabe eines Redakteurs hochgeladen.
- Die Freigabe setzt die TikTok-UX-Pflichten serverseitig durch (Sichtbarkeit ohne Voreinstellung,
  Interaktionen nur nach Wahl und nur wenn erlaubt, Kennzeichnung kommerzieller Inhalte, Einwilligung).
- Direct Post nur mit TIKTOK_DIRECT_POST_ENABLED und Scope video.publish; sonst Inbox-Upload oder Export.
- Rechte werden bei Warteschlange, Freigabe und unmittelbar vor dem Upload geprüft (ensure_can_publish).
- Nach erfolgreicher Initialisierung (publish_id) wird nie automatisch neu initialisiert → keine Doppel-Posts.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import (
    ActivityCategory,
    ActivityLevel,
    HighlightStatus,
    PublishMethod,
    PublishStatus,
)
from app.integrations.tiktok.client import MB, CreatorInfo, plan_chunks
from app.integrations.tiktok.errors import explain
from app.media.metadata import TIKTOK_CAPTION_LIMIT, caption_text
from app.models import Highlight, PublishJob, RenderedVideo, Streamer, TikTokAccount, User
from app.services import tiktok_accounts
from app.services.activity import log_activity
from app.services.streamers import ensure_can_publish
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

OPEN_STATUSES = (
    PublishStatus.PENDING_REVIEW.value,
    PublishStatus.APPROVED.value,
    PublishStatus.UPLOADING.value,
    PublishStatus.PROCESSING.value,
)
STATUS_CHECK_DELAYS = (15, 30, 60, 120, 300, 600, 900, 1800, 3600)

# Von TikTok vorgegebene Einwilligungs- und Kennzeichnungstexte (Content Sharing Guidelines)
MUSIC_CONSENT = "By posting, you agree to TikTok's Music Usage Confirmation."
BRANDED_CONSENT = "By posting, you agree to TikTok's Branded Content Policy and Music Usage Confirmation."
LABEL_YOUR_BRAND = 'Your photo/video will be labeled as "Promotional content".'
LABEL_BRANDED = 'Your photo/video will be labeled as "Paid partnership".'
MUSIC_URL = "https://www.tiktok.com/legal/page/global/music-usage-confirmation/en"
BRANDED_URL = "https://www.tiktok.com/legal/page/global/bc-policy/en"


class PublishError(ValueError):
    """Meldung für das Dashboard."""


# ---------------------------------------------------------------------------
# Warteschlange
# ---------------------------------------------------------------------------
def open_job_for(db: Session, highlight: Highlight) -> PublishJob | None:
    return db.scalar(
        select(PublishJob).where(
            PublishJob.highlight_id == highlight.id, PublishJob.status.in_(OPEN_STATUSES)
        )
    )


def add_to_queue(db: Session, settings: Settings, highlight: Highlight, *, actor: User) -> PublishJob:
    streamer = db.get(Streamer, highlight.streamer_id)
    try:
        ensure_can_publish(streamer)
    except PermissionError as exc:
        raise PublishError(str(exc)) from exc
    if open_job_for(db, highlight) is not None:
        raise PublishError("Dieses Highlight ist bereits in der Warteschlange.")
    if highlight.status == HighlightStatus.PUBLISHED:
        raise PublishError("Dieses Highlight wurde bereits veröffentlicht oder exportiert.")
    video = highlight.current_video
    if highlight.status != HighlightStatus.RENDERED or video is None or not Path(video.file_path).is_file():
        raise PublishError("Es gibt noch kein fertig geschnittenes Video.")
    spikes = (highlight.analysis or {}).get("audio_spikes") or []
    job = PublishJob(
        highlight_id=highlight.id,
        rendered_video_id=video.id,
        method=PublishMethod.MANUAL_EXPORT.value,  # endgültig erst bei der Freigabe
        caption=caption_text(highlight.description or highlight.title or "", highlight.hashtags or []),
        hashtags=list(highlight.hashtags or []),
        status=PublishStatus.PENDING_REVIEW.value,
        video_cover_timestamp_ms=int((spikes[0] + 0.5) * 1000) if spikes else 1000,
    )
    db.add(job)
    highlight.status = HighlightStatus.QUEUED.value
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.TIKTOK,
        f"Highlight #{highlight.id} in die TikTok-Warteschlange gestellt (Auftrag #{job.id})",
        user_id=actor.id,
        streamer_id=streamer.id,
    )
    return job


def available_methods(settings: Settings, account: TikTokAccount | None) -> list[str]:
    methods: list[str] = []
    if not settings.test_mode and account is not None and account.is_active:
        token = tiktok_accounts.active_token(account)
        scopes = set(token.scopes or []) if token else set()
        if settings.tiktok_direct_post_enabled and "video.publish" in scopes:
            methods.append(PublishMethod.DIRECT_POST.value)
        if settings.tiktok_inbox_upload_enabled and "video.upload" in scopes:
            methods.append(PublishMethod.INBOX_UPLOAD.value)
    methods.append(PublishMethod.MANUAL_EXPORT.value)
    return methods


# ---------------------------------------------------------------------------
# Freigabe (UX-Pflichten der TikTok Content Sharing Guidelines)
# ---------------------------------------------------------------------------
@dataclass
class ApprovalForm:
    account_id: int | None
    method: str
    caption: str
    privacy_level: str | None
    allow_comment: bool
    allow_duet: bool
    allow_stitch: bool
    commercial: bool
    your_brand: bool
    branded_content: bool
    is_aigc: bool
    consent: bool
    rights_confirmed: bool


def consent_text(branded_content: bool) -> str:
    return BRANDED_CONSENT if branded_content else MUSIC_CONSENT


def approve(
    db: Session,
    settings: Settings,
    job: PublishJob,
    form: ApprovalForm,
    creator_info: CreatorInfo | None,
    *,
    actor: User,
) -> PublishJob:
    if job.status != PublishStatus.PENDING_REVIEW:
        raise PublishError("Dieser Eintrag wurde bereits bearbeitet.")
    highlight = db.get(Highlight, job.highlight_id)
    streamer = db.get(Streamer, highlight.streamer_id)
    try:
        ensure_can_publish(streamer)
    except PermissionError as exc:
        raise PublishError(str(exc)) from exc
    video = db.get(RenderedVideo, job.rendered_video_id) if job.rendered_video_id else None
    if video is None or video.deleted_at is not None or not Path(video.file_path).is_file():
        raise PublishError("Die Videodatei ist nicht mehr vorhanden – bitte neu schneiden.")
    if not form.rights_confirmed:
        raise PublishError(
            "Bitte bestätigen, dass die Veröffentlichung von der Erlaubnis des Streamers gedeckt ist."
        )
    if not form.consent:
        raise PublishError("Bitte der Einwilligungserklärung zustimmen.")
    caption = form.caption.strip()
    if not caption:
        raise PublishError("Die Beschreibung darf nicht leer sein.")
    if len(caption) > TIKTOK_CAPTION_LIMIT:
        raise PublishError(f"Die Beschreibung ist länger als {TIKTOK_CAPTION_LIMIT} Zeichen.")

    account = db.get(TikTokAccount, form.account_id) if form.account_id else None
    if form.method not in available_methods(settings, account):
        raise PublishError("Dieser Veröffentlichungsweg ist für das gewählte Konto nicht verfügbar.")

    if form.method in (PublishMethod.DIRECT_POST, PublishMethod.INBOX_UPLOAD):
        if creator_info is None:
            raise PublishError("Die aktuellen Kontoinformationen von TikTok fehlen – Seite neu laden.")
        limit = creator_info.max_video_post_duration_sec
        if limit and video.duration > limit:
            raise PublishError(
                f"Das Video ({video.duration:.0f} s) ist länger als für dieses Konto erlaubt ({limit} s)."
            )

    job.privacy_level = None
    job.disable_comment = job.disable_duet = job.disable_stitch = False
    job.brand_content_toggle = job.brand_organic_toggle = False
    if form.method == PublishMethod.DIRECT_POST:
        if not form.privacy_level:
            raise PublishError("Bitte die Sichtbarkeit auswählen (es gibt bewusst keine Voreinstellung).")
        if form.privacy_level not in creator_info.privacy_level_options:
            raise PublishError("Diese Sichtbarkeit bietet TikTok für das Konto derzeit nicht an.")
        if form.commercial and not (form.your_brand or form.branded_content):
            raise PublishError("Bitte angeben, ob es um Ihre eigene Marke und/oder um Markeninhalte geht.")
        if form.commercial and form.branded_content and form.privacy_level == "SELF_ONLY":
            raise PublishError(
                "Markeninhalte (bezahlte Partnerschaft) dürfen nicht privat veröffentlicht werden."
            )
        job.privacy_level = form.privacy_level
        # Vom Creator deaktivierte Interaktionen bleiben immer aus
        job.disable_comment = creator_info.comment_disabled or not form.allow_comment
        job.disable_duet = creator_info.duet_disabled or not form.allow_duet
        job.disable_stitch = creator_info.stitch_disabled or not form.allow_stitch
        job.brand_organic_toggle = form.commercial and form.your_brand
        job.brand_content_toggle = form.commercial and form.branded_content
        job.is_aigc = form.is_aigc

    job.method = form.method
    job.tiktok_account_id = account.id if account and form.method != PublishMethod.MANUAL_EXPORT else None
    job.caption = caption
    job.consent_confirmed = True
    job.approved_by_user_id = actor.id
    job.approved_at = utcnow()
    job.status = PublishStatus.APPROVED.value
    job.fail_reason = None
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.TIKTOK,
        f"Auftrag #{job.id} freigegeben: {form.method}"
        + (f", Sichtbarkeit {job.privacy_level}" if job.privacy_level else "")
        + (f", Konto „{account.display_name}“" if job.tiktok_account_id else ""),
        user_id=actor.id,
        streamer_id=streamer.id,
    )
    return job


# ---------------------------------------------------------------------------
# Ausführung
# ---------------------------------------------------------------------------
def _fail(db: Session, job: PublishJob, reason: str) -> None:
    job.status = PublishStatus.FAILED.value
    job.fail_reason = reason[:2000]
    highlight = db.get(Highlight, job.highlight_id)
    if highlight is not None and highlight.status == HighlightStatus.QUEUED:
        highlight.status = HighlightStatus.RENDERED.value
    db.flush()
    log_activity(
        db,
        ActivityLevel.ERROR,
        ActivityCategory.TIKTOK,
        f"TikTok-Auftrag #{job.id} fehlgeschlagen: {reason}",
        streamer_id=highlight.streamer_id if highlight else None,
    )


def _done(db: Session, job: PublishJob, status: PublishStatus) -> None:
    job.status = status.value
    job.published_at = utcnow()
    job.next_check_at = None
    highlight = db.get(Highlight, job.highlight_id)
    if highlight is not None:
        highlight.status = HighlightStatus.PUBLISHED.value
    db.flush()


def check_rights_before_upload(db: Session, job: PublishJob) -> Streamer:
    highlight = db.get(Highlight, job.highlight_id)
    streamer = db.get(Streamer, highlight.streamer_id)
    ensure_can_publish(streamer)  # wirft RightsError
    return streamer


def create_export(db: Session, settings: Settings, job: PublishJob) -> Path:
    """Manueller Weg: ZIP mit Video, Beschreibung, Untertiteln und Hinweisen zum Hochladen in der App."""
    video = db.get(RenderedVideo, job.rendered_video_id)
    if video is None or not Path(video.file_path).is_file():
        raise PublishError("Die Videodatei ist nicht mehr vorhanden.")
    highlight = db.get(Highlight, job.highlight_id)
    streamer = db.get(Streamer, highlight.streamer_id)
    target = settings.exports_dir / str(streamer.id) / f"tiktok-export-{job.id}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    notes = (
        "TwitchClip AI – Export für den manuellen TikTok-Upload\n\n"
        f"Kanal: twitch.tv/{streamer.twitch_login}\n"
        f"Freigegeben am: {job.approved_at:%d.%m.%Y %H:%M} UTC\n\n"
        "1. video.mp4 in der TikTok-App hochladen.\n"
        "2. Text aus beschreibung.txt übernehmen.\n"
        "3. Optional untertitel.srt verwenden (die Untertitel sind bereits im Video eingebrannt).\n"
        "4. Keine urheberrechtlich geschützte Musik hinzufügen, für die keine Lizenz besteht.\n"
        "5. Sichtbarkeit, Kommentare/Duett/Stitch und ggf. Werbekennzeichnung in der App selbst wählen.\n"
    )
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(video.file_path, "video.mp4")
        archive.writestr("beschreibung.txt", job.caption)
        srt = Path(video.file_path).with_suffix(".srt")
        if srt.is_file():
            archive.write(srt, "untertitel.srt")
        archive.writestr("HINWEISE.txt", notes)
    job.export_path = str(target)
    _done(db, job, PublishStatus.EXPORTED)
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.TIKTOK,
        f"Auftrag #{job.id} als Export bereitgestellt",
        streamer_id=streamer.id,
    )
    return target


def post_info(job: PublishJob) -> dict:
    return {
        "title": job.caption,
        "privacy_level": job.privacy_level,
        "disable_duet": job.disable_duet,
        "disable_comment": job.disable_comment,
        "disable_stitch": job.disable_stitch,
        "video_cover_timestamp_ms": job.video_cover_timestamp_ms or 1000,
        "brand_content_toggle": job.brand_content_toggle,
        "brand_organic_toggle": job.brand_organic_toggle,
        "is_aigc": job.is_aigc,
    }


@dataclass
class InitResult:
    publish_id: str
    upload_url: str
    plan: object


def initialize_upload(db: Session, settings: Settings, http: httpx.Client, job: PublishJob) -> InitResult:
    """Schritt 1: Creator-Info erneut prüfen und bei TikTok initialisieren (liefert publish_id)."""
    account = db.get(TikTokAccount, job.tiktok_account_id)
    if account is None or not account.is_active:
        raise PublishError("Das TikTok-Konto ist nicht mehr verbunden.")
    info = tiktok_accounts.refresh_creator_info(db, settings, http, account)  # Pflicht direkt vor dem Post
    video = db.get(RenderedVideo, job.rendered_video_id)
    path = Path(video.file_path)
    if job.method == PublishMethod.DIRECT_POST and job.privacy_level not in info.privacy_level_options:
        raise PublishError(
            "Die gewählte Sichtbarkeit ist für das Konto nicht mehr verfügbar – bitte neu freigeben."
        )
    if info.max_video_post_duration_sec and video.duration > info.max_video_post_duration_sec:
        raise PublishError("Das Video ist länger als für dieses Konto erlaubt.")
    plan = plan_chunks(path.stat().st_size, settings.tiktok_chunk_size_mb * MB)
    client = tiktok_accounts.client_for(db, settings, http, account)
    if job.method == PublishMethod.DIRECT_POST:
        init = client.init_direct_post(post_info(job), plan)
    else:
        init = client.init_inbox_upload(plan)
    job.publish_id = init.publish_id
    job.status = PublishStatus.UPLOADING.value
    job.attempts += 1
    job.uploaded_bytes = 0
    db.flush()
    return InitResult(publish_id=init.publish_id, upload_url=init.upload_url or "", plan=plan)


def upload(db: Session, settings: Settings, http: httpx.Client, job: PublishJob, init: InitResult) -> None:
    """Schritt 2: Datei in Stücken übertragen; danach verarbeitet TikTok das Video."""
    account = db.get(TikTokAccount, job.tiktok_account_id)
    video = db.get(RenderedVideo, job.rendered_video_id)
    client = tiktok_accounts.client_for(db, settings, http, account)

    def progress(sent: int) -> None:
        job.uploaded_bytes = sent
        db.commit()  # Fortschritt sofort im Dashboard sichtbar

    client.upload_file(init.upload_url, Path(video.file_path), init.plan, progress=progress)
    job.status = PublishStatus.PROCESSING.value
    job.next_check_at = utcnow() + timedelta(seconds=STATUS_CHECK_DELAYS[0])
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.TIKTOK,
        f"Auftrag #{job.id} an TikTok übertragen ({video.size_bytes / 1024**2:.1f} MB) – "
        "TikTok verarbeitet das Video",
        streamer_id=video.highlight.streamer_id,
    )


def apply_status(db: Session, job: PublishJob, status, account: TikTokAccount | None) -> bool:
    """Wertet die Statusantwort aus. True = Endzustand erreicht."""
    job.tiktok_status = status.status
    job.last_checked_at = utcnow()
    if status.uploaded_bytes is not None:
        job.uploaded_bytes = status.uploaded_bytes
    if status.status == "PUBLISH_COMPLETE":
        if status.post_ids:
            job.tiktok_post_id = status.post_ids[0]
        _done(db, job, PublishStatus.PUBLISHED)
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.TIKTOK,
            f"Auftrag #{job.id} auf TikTok veröffentlicht"
            + (f" (Post {job.tiktok_post_id})" if job.tiktok_post_id else ""),
        )
        return True
    if status.status == "SEND_TO_USER_INBOX":
        _done(db, job, PublishStatus.SENT_TO_INBOX)
        log_activity(
            db,
            ActivityLevel.INFO,
            ActivityCategory.TIKTOK,
            f"Auftrag #{job.id} liegt in der TikTok-Inbox von "
            f"„{account.display_name if account else '?'}“ – "
            "dort in der App fertigstellen und veröffentlichen",
        )
        return True
    if status.status == "FAILED":
        _fail(db, job, explain(status.fail_reason, status.fail_reason))
        return True
    job.status = PublishStatus.PROCESSING.value
    db.flush()
    return False


def reset_for_retry(db: Session, job: PublishJob, *, actor: User) -> None:
    """Fehlgeschlagenen Auftrag zurück zur Freigabe (bewusst erneut prüfen statt automatisch neu posten)."""
    if job.status != PublishStatus.FAILED:
        raise PublishError("Nur fehlgeschlagene Aufträge können erneut vorbereitet werden.")
    highlight = db.get(Highlight, job.highlight_id)
    if open_job_for(db, highlight) is not None:
        raise PublishError("Für dieses Highlight gibt es bereits einen offenen Auftrag.")
    job.status = PublishStatus.PENDING_REVIEW.value
    job.publish_id = None
    job.tiktok_status = None
    job.uploaded_bytes = None
    job.approved_at = None
    job.approved_by_user_id = None
    job.consent_confirmed = False
    highlight.status = HighlightStatus.QUEUED.value
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.TIKTOK,
        f"Auftrag #{job.id} erneut zur Freigabe gestellt",
        user_id=actor.id,
        streamer_id=highlight.streamer_id,
    )
