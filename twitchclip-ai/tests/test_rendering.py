import re
import shutil
from pathlib import Path

import pytest

from app.media import layout as lay
from app.media.metadata import build_metadata, caption_text, hashtag, normalize_hashtags
from app.media.subtitles import build_cues, render_ass, to_srt
from app.models import Highlight, PublishJob, RenderedVideo
from app.services.media_probe import probe_media
from tests import factories
from tests.conftest import post_form

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
WORDS = [
    {"start": 0.5, "end": 0.9, "text": "Alter,"},
    {"start": 1.0, "end": 1.3, "text": "was"},
    {"start": 1.35, "end": 1.7, "text": "war"},
    {"start": 1.75, "end": 2.2, "text": "DAS?!"},
    {"start": 4.0, "end": 4.4, "text": "Unglaublich"},
]


def _crops(graph: str) -> list[tuple[int, int, int, int]]:
    return [tuple(map(int, m)) for m in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", graph)]


# --- Layout --------------------------------------------------------------------
def test_layout_selection():
    cam = {"x": 0.72, "y": 0.70, "w": 0.26, "h": 0.26, "confidence": 0.9}
    assert lay.choose_layout(1920, 1080, cam).name == "facecam_split"
    assert lay.choose_layout(1920, 1080, None).name == "blur_fill"
    assert lay.choose_layout(1920, 1080, cam, prefer="blur_fill").name == "blur_fill"
    assert lay.choose_layout(1920, 1080, dict(cam, confidence=0.1)).name == "blur_fill"
    assert lay.choose_layout(1080, 1920, cam).name == "vertical"
    with pytest.raises(ValueError):
        lay.choose_layout(0, 0, None)


@pytest.mark.parametrize(
    "cam",
    [
        {"x": 0.72, "y": 0.70, "w": 0.26, "h": 0.26, "confidence": 0.9},
        {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.3, "confidence": 0.9},
        {"x": 0.5, "y": 0.9, "w": 0.1, "h": 0.1, "confidence": 0.9},
    ],
)
def test_facecam_crops_stay_inside_source(cam):
    result = lay.facecam_split(1920, 1080, cam)
    for w, h, x, y in _crops(result.filter_graph):
        assert w > 0 and h > 0 and x >= 0 and y >= 0
        assert x + w <= 1920 + 1 and y + h <= 1080 + 1
    heights = [int(v) for v in re.findall(r"scale=1080:(\d+)", result.filter_graph)]
    assert sum(heights) == 1920 and lay.CAM_MIN_H <= heights[0] <= lay.CAM_MAX_H
    # Facecam rechts → Gameplay-Ausschnitt nach links verschoben
    if cam["x"] > 0.6:
        game_x = _crops(result.filter_graph)[1][2]
        assert game_x < (1920 - _crops(result.filter_graph)[1][0]) / 2


# --- Untertitel ----------------------------------------------------------------
def test_cues_are_short_and_split_on_pause():
    cues = build_cues(WORDS, 10)
    assert [c.text for c in cues] == ["Alter, was war", "DAS?!", "Unglaublich"]
    assert all(len(c.text) <= 22 for c in cues)
    assert all(a.end <= b.start for a, b in zip(cues, cues[1:], strict=False))
    assert cues[-1].end <= 10


def test_ass_contains_karaoke_title_and_resolution():
    ass = render_ass(WORDS, 10, lay.blur_fill(1920, 1080), title="Krasser Clutch")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert re.search(r"\{\\k\d+\}Alter,", ass)
    title_line = next(line for line in ass.splitlines() if ",Title," in line)
    assert "0:00:10.00" in title_line  # blur_fill: Überschrift bleibt die ganze Zeit
    facecam_ass = render_ass(
        WORDS, 10, lay.facecam_split(1920, 1080, {"x": 0.7, "y": 0.7, "w": 0.2, "h": 0.2}), title="X {bad}"
    )
    title_line = next(line for line in facecam_ass.splitlines() if ",Title," in line)
    assert "0:00:03.50" in title_line and "{bad}" not in title_line
    assert render_ass([], 10, lay.blur_fill(1920, 1080)).count("Dialogue") == 0


def test_srt_format():
    srt = to_srt(WORDS, 10)
    assert srt.startswith("1\n00:00:00,500 --> ")
    assert "\n2\n00:00:01,750 --> " in srt


# --- Metadaten -----------------------------------------------------------------
def test_metadata_title_description_and_max_five_hashtags():
    meta = build_metadata(
        streamer_name="Kanal Eins",
        streamer_login="kanal_eins",
        language="de",
        source_title="Unglaublicher 1v5 Clutch",
        words=WORDS,
        game="Counter-Strike 2",
    )
    assert meta.title == "Unglaublicher 1v5 Clutch"
    assert meta.hashtags == ["kanaleins", "counterstrike2", "twitch", "gaming", "twitchdeutschland"]
    assert "twitch.tv/kanal_eins" in meta.description and "Counter-Strike 2" in meta.description
    generic = build_metadata(
        streamer_name="Kanal",
        streamer_login="kanal",
        language="en",
        source_title="Kanal – Highlight",
        words=WORDS,
        game=None,
    )
    assert generic.title.startswith("„Alter, was war DAS?!“") and "Live on Twitch" in generic.description
    fallback = build_metadata(
        streamer_name="Kanal", streamer_login="kanal", language="de", source_title=None, words=[], game=None
    )
    assert fallback.title == "Krasser Moment bei Kanal"
    long = build_metadata(
        streamer_name="K",
        streamer_login="k",
        language="de",
        source_title="x" * 30 + " " + "y" * 40,
        words=None,
        game=None,
    )
    assert len(long.title) <= 60 and long.title.endswith("…")


def test_hashtag_helpers():
    assert hashtag("Pokémon Karmesin") == "pokemonkarmesin"
    assert normalize_hashtags("#Twitch, gaming #Gaming a b c d") == ["twitch", "gaming", "a", "b", "c"]
    caption = caption_text("Titel\nZeile 2", ["a", "b", "c", "d", "e", "f"])
    assert caption.endswith("#a #b #c #d #e") and len(caption) <= 2200


# --- Rendering mit FFmpeg --------------------------------------------------------
def _sample_highlight(db, settings, *, facecam=None, words=WORDS):
    from app.services.clip_processing import ingest_samples
    from app.services.samples import generate_samples

    generate_samples(settings, count=1, duration=20)
    (request,) = ingest_samples(db, settings)
    return factories.highlight(
        db,
        request.streamer,
        score=80,
        status="candidate",
        source_ref="render-test",
        clip_request_id=request.id,
        source_file_path=request.file_path,
        start_seconds=3,
        end_seconds=13,
        title="beispiel-01-20s",
        description=None,
        analysis={"words": words, "facecam": facecam, "audio_spikes": [2.0]},
    )


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg nicht installiert")
@pytest.mark.parametrize("facecam", [None, {"x": 0.72, "y": 0.70, "w": 0.26, "h": 0.26, "confidence": 0.9}])
def test_render_highlight_produces_tiktok_ready_mp4(settings, db_session, monkeypatch, facecam):
    from app.services.rendering import render_highlight

    monkeypatch.setattr(settings, "test_mode", True)
    monkeypatch.setattr(settings, "render_preset", "ultrafast")
    highlight = _sample_highlight(db_session, settings, facecam=facecam)
    video = render_highlight(db_session, settings, highlight)
    info = probe_media(Path(video.file_path))
    assert (info["width"], info["height"], info["video_codec"], info["audio_codec"]) == (
        1080,
        1920,
        "h264",
        "aac",
    )
    assert info["fps"] == 30 and 9.5 <= info["duration"] <= 10.5
    assert video.layout == ("facecam_split" if facecam else "blur_fill")
    assert Path(video.thumbnail_path).is_file() and Path(video.file_path).with_suffix(".srt").is_file()
    assert highlight.status == "rendered" and highlight.current_video.id == video.id
    assert highlight.title.startswith("„Alter, was war DAS?!“") and len(highlight.hashtags) <= 5
    assert Path(video.file_path).is_relative_to(settings.renders_dir)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg nicht installiert")
def test_rerender_cleans_up_but_keeps_queued_version(settings, db_session, monkeypatch):
    from app.services.rendering import render_highlight

    monkeypatch.setattr(settings, "test_mode", True)
    monkeypatch.setattr(settings, "render_preset", "ultrafast")
    highlight = _sample_highlight(db_session, settings)
    first = render_highlight(db_session, settings, highlight)
    second = render_highlight(db_session, settings, highlight)
    assert first.deleted_at is not None and not Path(first.file_path).exists()
    assert not Path(first.file_path).with_suffix(".srt").exists()
    factories.publish_job(db_session, highlight, second, status="pending_review")
    third = render_highlight(db_session, settings, highlight)
    assert second.deleted_at is None and Path(second.file_path).exists()  # steckt in der Warteschlange
    assert highlight.current_video.id == third.id


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg nicht installiert")
def test_render_respects_rights_and_missing_files(settings, db_session, monkeypatch):
    from app.services.rendering import RenderingError, render_highlight
    from app.services.streamers import RightsError

    monkeypatch.setattr(settings, "test_mode", True)
    highlight = _sample_highlight(db_session, settings)
    monkeypatch.setattr(settings, "test_mode", False)  # Demo-Kanal ohne Testmodus → keine Rechte
    with pytest.raises(RightsError):
        render_highlight(db_session, settings, highlight)
    monkeypatch.setattr(settings, "test_mode", True)
    Path(highlight.source_file_path).unlink()
    with pytest.raises(RenderingError, match="nicht"):
        render_highlight(db_session, settings, highlight)
    highlight.status = "published"
    with pytest.raises(RenderingError, match="Status"):
        render_highlight(db_session, settings, highlight)


def test_render_job_reports_failure(settings, database, db_session, monkeypatch):
    from app.workers.jobs.render import render_highlight_job

    monkeypatch.setattr(settings, "test_mode", True)
    s = factories.streamer(db_session, "demo_x", is_demo=True, mode="authorized")
    h = factories.highlight(db_session, s, score=70, status="candidate", source_file_path="/nicht/da.mp4")
    db_session.commit()
    assert "error" in render_highlight_job(h.id)
    db_session.expire_all()
    assert (
        db_session.get(Highlight, h.id).status == "candidate"
    )  # Quelle fehlt → Status unverändert, Fehler protokolliert


# --- Dashboard -----------------------------------------------------------------
def test_dashboard_edit_texts_and_queue_render(admin_client, db_session, settings, redis, monkeypatch):
    from app.workers.queues import QUEUE_MEDIA, get_queue

    monkeypatch.setattr(settings, "test_mode", True)
    s = factories.streamer(db_session, "demo_x", is_demo=True, mode="authorized")
    h = factories.highlight(db_session, s, score=70, status="candidate")
    db_session.commit()
    page = admin_client.get(f"/highlights/{h.id}").text
    assert "Überschrift, Beschreibung" in page and "Video erstellen" in page
    response = post_form(
        admin_client,
        f"/highlights/{h.id}/metadata",
        {"title": "Mein Titel", "description": "Text", "hashtags": "#A b,c d e f g"},
    )
    assert response.status_code == 303
    db_session.expire_all()
    h = db_session.get(Highlight, h.id)
    assert (h.title, h.description, h.hashtags) == ("Mein Titel", "Text", ["a", "b", "c", "d", "e"])
    post_form(
        admin_client, f"/highlights/{h.id}/metadata", {"title": "x" * 70, "description": "", "hashtags": ""}
    )
    assert db_session.get(Highlight, h.id).title == "Mein Titel"  # zu lang → abgelehnt
    post_form(admin_client, f"/highlights/{h.id}/render", {"layout": "blur_fill"})
    assert any(j.startswith(f"render-{h.id}-") for j in get_queue(QUEUE_MEDIA, redis).job_ids)


def test_srt_download_requires_login(client, make_user, db_session, settings):
    from tests.conftest import login

    make_user()
    s = factories.streamer(db_session, "kanal")
    h = factories.highlight(db_session, s)
    video = factories.rendered_video(db_session, h, settings.renders_dir / "v.mp4")
    Path(video.file_path).with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHallo\n")
    db_session.commit()
    assert client.get(f"/media/videos/{video.id}/subtitles.srt", follow_redirects=False).status_code == 303
    login(client)
    response = client.get(f"/media/videos/{video.id}/subtitles.srt")
    assert response.status_code == 200 and "Hallo" in response.text


def test_selected_highlights_are_rendered_automatically(settings, database, db_session, redis, monkeypatch):
    from app.services import highlight_analysis as ha
    from app.workers.jobs import analysis as analysis_jobs
    from app.workers.queues import QUEUE_MEDIA, get_queue

    monkeypatch.setattr(settings, "min_highlight_score", 10)
    s = factories.streamer(db_session, "kanal")
    request_row = factories.highlight(
        db_session, s, score=80, status="candidate", selection_group="samples-x"
    )
    from app.models import ClipRequest

    cr = ClipRequest(
        streamer_id=s.id, source="sample", dedup_key="sample:x", status="downloaded", file_path=__file__
    )
    db_session.add(cr)
    db_session.commit()
    monkeypatch.setattr(
        analysis_jobs, "analyze_clip_request", lambda db, st, req, tr: db.get(Highlight, request_row.id)
    )
    monkeypatch.setattr(analysis_jobs, "get_transcriber", lambda st: None)
    monkeypatch.setattr(ha, "group_is_complete", lambda db, g: True)
    result = analysis_jobs.analyze_clip_job(cr.id)
    assert result["selected"] == [request_row.id]
    assert f"render-{request_row.id}" in get_queue(QUEUE_MEDIA, redis).job_ids
    assert PublishJob.__tablename__ and RenderedVideo.__tablename__
