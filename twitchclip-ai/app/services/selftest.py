"""End-to-End-Selbsttest im Testmodus (Phase 7).

Durchläuft die komplette Kette mit frisch erzeugten, synthetischen Videos – ohne Twitch, ohne TikTok:
Beispielvideos → Übernahme → KI-Analyse → Auswahl → Schnitt (1080×1920) → Freigabe → ZIP-Export.

Aufruf auf dem Server (Container mit FFmpeg und Whisper-Modell):
    docker compose exec worker-media python -m app.cli selftest
"""

from __future__ import annotations

import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from app.ai.transcribe import get_transcriber
from app.config import Settings
from app.db import session_scope
from app.enums import HighlightStatus, UserRole
from app.models import ClipRequest, Highlight, User
from app.services import publishing
from app.services.clip_processing import ingest_samples
from app.services.highlight_analysis import analyze_clip_request, group_is_complete, select_group
from app.services.media_probe import probe_media
from app.services.rendering import render_highlight
from app.services.samples import generate_samples
from app.timeutil import utcnow

EXPORT_FILES = {"video.mp4", "beschreibung.txt", "HINWEISE.txt"}


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    seconds: float


@dataclass
class SelftestResult:
    steps: list[Step] = field(default_factory=list)
    highlight_id: int | None = None
    job_id: int | None = None

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)


class _Abort(Exception):
    pass


def run_selftest(
    settings: Settings,
    *,
    transcribe: bool = True,
    duration: int = 40,
    count: int = 2,
    report: Callable[[Step], None] | None = None,
) -> SelftestResult:
    result = SelftestResult()

    def step(name: str, func: Callable[[], str]) -> None:
        started = time.monotonic()
        try:
            detail, ok = func(), True
        except _Abort as exc:
            detail, ok = str(exc), False
        except Exception as exc:  # noqa: BLE001 – jeder Fehler ist ein Testergebnis
            detail, ok = f"{exc.__class__.__name__}: {exc}", False
        item = Step(name, ok, detail, round(time.monotonic() - started, 1))
        result.steps.append(item)
        if report:
            report(item)
        if not ok:
            raise _Abort(detail)

    tag = f"selbsttest-{utcnow():%Y%m%d-%H%M%S}"
    state: dict = {}

    def prerequisites() -> str:
        if not settings.test_mode:
            raise _Abort("TEST_MODE ist nicht aktiv – nur im Testmodus ausführen (TEST_MODE=true).")
        missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
        if missing:
            raise _Abort(f"Nicht installiert: {', '.join(missing)} (im Container worker-media ausführen)")
        with session_scope() as db:
            admin = db.scalar(
                select(User)
                .where(User.role == UserRole.ADMIN.value, User.is_active.is_(True))
                .order_by(User.id)
            )
            if admin is None:
                raise _Abort("Kein aktiver Admin vorhanden (python -m app.cli bootstrap).")
            state["admin_id"] = admin.id
        return f"Testmodus aktiv, FFmpeg vorhanden, Freigabe im Namen von Benutzer #{state['admin_id']}"

    def make_samples() -> str:
        files = generate_samples(settings, count=count, duration=duration, tag=tag)
        state["files"] = {str(p) for p in files}
        return f"{len(files)} synthetische Videos à {duration} s ({tag})"

    def ingest() -> str:
        with session_scope() as db:
            created = [r for r in ingest_samples(db, settings) if r.file_path in state["files"]]
            db.flush()
            state["requests"] = [r.id for r in created]
        if len(state["requests"]) != count:
            raise _Abort(f"{len(state['requests'])} statt {count} Aufträge übernommen")
        return f"Aufträge #{', #'.join(map(str, state['requests']))} (Status „heruntergeladen“)"

    def analyze() -> str:
        transcriber = get_transcriber(settings) if transcribe else None
        scores = []
        state["highlights"] = []
        for request_id in state["requests"]:
            with session_scope() as db:
                request = db.get(ClipRequest, request_id)
                highlight = analyze_clip_request(db, settings, request, transcriber)
                db.flush()
                state["highlights"].append(highlight.id)
                state["group"] = highlight.selection_group
                scores.append(f"#{highlight.id}: {highlight.score:.0f}")
        speech = "mit Spracherkennung" if transcriber else "ohne Spracherkennung"
        return f"Bewertungen {', '.join(scores)} ({speech})"

    def choose() -> str:
        with session_scope() as db:
            ours = set(state["highlights"])
            chosen = []
            if state.get("group") and group_is_complete(db, state["group"]):
                chosen = [h.id for h in select_group(db, settings, state["group"]) if h.id in ours]
            if chosen:
                state["best"] = chosen[0]
                return f"Ausgewählt: #{', #'.join(map(str, chosen))}"
            best = max(
                db.scalars(select(Highlight).where(Highlight.id.in_(ours))).all(),
                key=lambda h: h.score,
                default=None,
            )
            if best is None:
                raise _Abort("Kein Highlight erkannt")
            best.status = HighlightStatus.CANDIDATE.value
            state["best"] = best.id
            return (
                "Keins über MIN_HIGHLIGHT_SCORE bzw. Tageslimit erreicht – "
                f"bestes (#{best.id}, {best.score:.0f}) wird für den Test trotzdem geschnitten"
            )

    def render() -> str:
        with session_scope() as db:
            highlight = db.get(Highlight, state["best"])
            if highlight.current_video is None:
                render_highlight(db, settings, highlight)
                db.flush()
            video = highlight.current_video
            info = probe_media(Path(video.file_path)) or {}
            state["video"] = video.file_path
        expected = (1080, 1920, "h264", "aac")
        actual = (info.get("width"), info.get("height"), info.get("video_codec"), info.get("audio_codec"))
        if actual != expected:
            raise _Abort(f"Ausgabe {actual} statt {expected}")
        if not 15 <= info.get("duration", 0) <= settings.clip_duration_max + 1:
            raise _Abort(f"Unerwartete Länge {info.get('duration')} s")
        return f"{info['width']}×{info['height']} H.264/AAC, {info['duration']:.1f} s, {info['fps']:.0f} fps"

    def publish() -> str:
        with session_scope() as db:
            admin = db.get(User, state["admin_id"])
            highlight = db.get(Highlight, state["best"])
            job = publishing.add_to_queue(db, settings, highlight, actor=admin)
            form = publishing.ApprovalForm(
                account_id=None, method="manual_export", caption=job.caption, privacy_level=None,
                allow_comment=False, allow_duet=False, allow_stitch=False, commercial=False, your_brand=False,
                branded_content=False, is_aigc=False, consent=True, rights_confirmed=True,
            )  # fmt: skip
            publishing.approve(db, settings, job, form, None, actor=admin)
            path = publishing.create_export(db, settings, job)
            state["job"] = job.id
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        if not names >= EXPORT_FILES:
            raise _Abort(f"Export unvollständig: {sorted(names)}")
        return f"Auftrag #{state['job']} freigegeben und exportiert ({path.stat().st_size / 1024**2:.1f} MB)"

    try:
        step("Voraussetzungen", prerequisites)
        step("Beispielvideos erzeugen", make_samples)
        step("Übernahme (Testmodus)", ingest)
        step("KI-Analyse", analyze)
        step("Auswahl", choose)
        step("Schnitt & Untertitel", render)
        step("Freigabe & Export", publish)
    except _Abort:
        pass
    result.highlight_id = state.get("best")
    result.job_id = state.get("job")
    return result
