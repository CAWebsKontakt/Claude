import json
import shutil
from datetime import timedelta

import numpy as np
import pytest

from app.ai import scoring
from app.ai.audio import analyze_samples
from app.ai.transcribe import Transcript, Word, set_transcriber, speech_excitement
from app.ai.vision import _facecam_region, dhash, hamming
from app.models import ChatActivity, ClipRequest, Highlight
from app.services import chat_stats
from app.services.highlight_analysis import SELECTION_PREFIX, analyze_clip_request, select_group
from app.timeutil import utcnow
from tests import factories

HAS_FFMPEG = shutil.which("ffmpeg") is not None


class FakeTranscriber:
    def transcribe(self, wav, language):
        return Transcript(
            "de", [Word(15.0, 15.4, "Alter"), Word(15.5, 15.9, "WAS!"), Word(16.0, 16.3, "krass")]
        )


# --- Signale -----------------------------------------------------------------
def test_audio_detects_loud_burst():
    rate = 16000
    t = np.arange(rate * 30) / rate
    signal = 0.02 * np.sin(2 * np.pi * 220 * t)
    signal[rate * 12 : rate * 15] *= 25  # lauter Ausbruch bei 12–15 s
    profile = analyze_samples(signal.astype(np.float32), rate)
    assert any(11.5 <= s <= 12.5 for s in profile.spikes)
    assert max(profile.excitement[12:15]) > 0.8 and max(profile.excitement[:10]) < 0.2


def test_speech_excitement_keywords_and_laughter():
    transcript = Transcript(
        "de", [Word(2.0, 2.3, "ganz"), Word(5.0, 5.3, "krass!"), Word(8.0, 8.4, "hahaha")]
    )
    values = speech_excitement(transcript, 10)
    assert values[5] > 0.5 and values[8] > 0.4 and values[2] < 0.2
    assert speech_excitement(None, 3) == [0.0, 0.0, 0.0]


def test_best_window_and_duration():
    signal = [0.0] * 120
    for i in range(70, 80):
        signal[i] = 1.0
    window = scoring.best_window(signal, 120, 20, 45)
    assert window.start <= 70 and window.end >= 80 and 20 <= window.duration <= 45
    short = scoring.best_window([0.5] * 25, 25, 20, 45)
    assert (short.start, short.end) == (0.0, 25)
    assert scoring.duration_fit(30, 20, 45) == 1.0 and scoring.duration_fit(10, 20, 45) < 1


def test_total_score_redistributes_missing_signals():
    full, _ = scoring.total_score({"ton": 1, "sprache": 1, "chat": 1, "bild": 1, "zuschauer": 1, "laenge": 1})
    partial, breakdown = scoring.total_score(
        {"ton": 1, "sprache": None, "chat": None, "bild": 1, "zuschauer": None, "laenge": 1}
    )
    assert full == partial == 100.0
    assert "Chat-Reaktion" in breakdown["nicht verfügbar"]
    assert scoring.chat_component(1.0) == 0.0 and scoring.chat_component(6.0) == 1.0
    assert scoring.chat_component(None) is None


def test_hashes_and_facecam_region():
    a = np.tile(np.arange(64, dtype=np.uint8), (64, 1))
    assert hamming(dhash(a), dhash(a)) == 0
    assert hamming(dhash(a), dhash(a[:, ::-1].copy())) > 30
    faces = [(0.80, 0.70, 0.06, 0.1)] * 8 + [(0.2, 0.2, 0.05, 0.08)]
    region = _facecam_region(faces, checks=10)
    assert region and region["x"] > 0.6 and region["y"] > 0.5 and region["confidence"] == 0.8
    assert _facecam_region([(0.5, 0.5, 0.1, 0.1)], checks=10) is None  # nur einmal gesehen → keine Facecam


# --- Chat-Statistik ------------------------------------------------------------
def test_chat_counts_without_storing_texts(db_session, redis):
    s = factories.streamer(db_session, "kanal", twitch_user_id="111")
    session = factories.stream_session(db_session, s, ended=False)
    db_session.commit()
    now = utcnow().timestamp() - 60
    for i in range(30):
        chat_stats.record_chat_message(
            redis,
            "111",
            f"user{i % 7}",
            "POGGERS das war krass" if i % 3 == 0 else "hi",
            1 if i % 2 else 0,
            now=now,
        )
    for key in redis.scan_iter("tcai:chat:*"):
        assert b"POGGERS" not in key and b"user" not in key
        if not key.endswith(b":u"):
            assert all(b"POGGERS" not in v for v in redis.hgetall(key).values())
    assert chat_stats.flush_chat_buckets(db_session, redis) == 1
    row = db_session.query(ChatActivity).one()
    assert (row.message_count, row.unique_chatters, row.hype_keyword_count, row.emote_count) == (
        30,
        7,
        10,
        15,
    )
    assert row.stream_session_id == session.id
    assert list(redis.scan_iter("tcai:chat:*")) == []


def _buckets(db, streamer, session, spikes_at: dict[int, int], base=5, length=600):
    for offset in range(0, length, 10):
        db.add(
            ChatActivity(
                streamer_id=streamer.id,
                stream_session_id=session.id,
                bucket_start=session.started_at + timedelta(seconds=offset),
                bucket_seconds=10,
                message_count=spikes_at.get(offset, base),
                unique_chatters=3,
                emote_count=0,
                hype_keyword_count=0,
            )
        )
    db.flush()


def test_chat_window_signal_and_spikes(db_session):
    s = factories.streamer(db_session, "kanal", twitch_user_id="111")
    session = factories.stream_session(db_session, s)
    _buckets(db_session, s, session, {300: 60, 310: 40, 500: 30})
    buckets = chat_stats.session_buckets(db_session, session)
    window = chat_stats.window_signal(buckets, session, 280, 305)
    assert window.ratio >= 10 and window.peak_per_minute == 360
    assert chat_stats.find_spikes(buckets, session, 2.5) == [300.0, 500.0]


def test_chat_spikes_become_vod_candidates(db_session, twitch_settings, make_user):
    from app.services.clip_processing import plan_chat_spike_clips

    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    _buckets(db_session, s, session, {300: 60, 500: 30})
    created = plan_chat_spike_clips(db_session, twitch_settings, session)
    assert [(r.source, r.vod_offset - r.duration, r.duration) for r in created] == [
        ("vod_window", 263, 45),
        ("vod_window", 463, 45),
    ]
    assert plan_chat_spike_clips(db_session, twitch_settings, session) == []  # keine Duplikate


def test_chat_webhook_counts_only(client, twitch_settings, redis, db_session):
    from tests.twitch_payloads import eventsub_headers

    body = json.dumps(
        {
            "subscription": {"id": "c", "type": "channel.chat.message", "status": "enabled"},
            "event": {
                "broadcaster_user_id": "111",
                "chatter_user_id": "42",
                "chatter_user_login": "geheim",
                "message": {"text": "KEKW was ein Play", "fragments": [{"type": "text"}, {"type": "emote"}]},
            },
        }
    ).encode()
    response = client.post(
        "/webhooks/twitch/eventsub",
        content=body,
        headers=eventsub_headers(body, "notification", message_id="chat-1"),
    )
    assert response.status_code == 204
    keys = [k for k in redis.scan_iter("tcai:chat:111:*") if not k.endswith(b":u")]
    assert len(keys) == 1
    assert {k: int(v) for k, v in redis.hgetall(keys[0]).items()} == {b"n": 1, b"e": 1, b"h": 1}
    assert db_session.query(ChatActivity).count() == 0


def test_eventsub_sync_adds_chat_subscription(db_session, twitch_settings, make_user, helix):
    import httpx
    import respx

    from app.services.twitch_monitor import sync_eventsub
    from tests.twitch_payloads import API, AUTH, subscription, token_response

    make_user()
    factories.authorized_streamer(
        db_session,
        twitch_settings,
        scopes=["channel:manage:clips", "user:read:chat", "user:bot", "channel:bot"],
    )
    db_session.commit()
    with respx.mock:
        respx.post(f"{AUTH}/token").mock(return_value=httpx.Response(200, json=token_response()))
        respx.get(f"{API}/eventsub/subscriptions").mock(
            return_value=httpx.Response(200, json={"data": [], "total_cost": 0, "max_total_cost": 10000})
        )
        bodies = []

        def create(request):
            data = json.loads(request.content)
            bodies.append(data)
            return httpx.Response(
                202,
                json={
                    "data": [
                        subscription(
                            f"s{len(bodies)}", data["type"], data["condition"].get("broadcaster_user_id", "")
                        )
                    ]
                },
            )

        respx.post(f"{API}/eventsub/subscriptions").mock(side_effect=create)
        sync_eventsub(db_session, helix, twitch_settings)
    chat = [b for b in bodies if b["type"] == "channel.chat.message"]
    assert chat and chat[0]["condition"] == {"broadcaster_user_id": "111", "user_id": "111"}


# --- Auswahl -----------------------------------------------------------------
def _highlight(db, streamer, score, group, status="candidate", error=None):
    return factories.highlight(
        db,
        streamer,
        score=score,
        status=status,
        source_ref=f"{group}-{score}",
        selection_group=group,
        error=error,
    )


def test_selection_top_n_threshold_and_manual_reject(db_session, settings, monkeypatch):
    monkeypatch.setattr(settings, "highlights_per_stream_max", 3)
    monkeypatch.setattr(settings, "min_highlight_score", 50)
    s = factories.streamer(db_session, "kanal")
    for score in (95, 85, 75, 65, 40):
        _highlight(db_session, s, score, "session-1")
    manual = _highlight(db_session, s, 99, "session-1", status="rejected")  # vom Redakteur verworfen
    selected = select_group(db_session, settings, "session-1")
    assert [h.score for h in selected] == [95, 85, 75]
    by_score = {h.score: h for h in db_session.query(Highlight).all()}
    assert by_score[65].status == "rejected" and "besten 3" in by_score[65].error
    assert (
        by_score[40].error.startswith(SELECTION_PREFIX) and "unter MIN_HIGHLIGHT_SCORE" in by_score[40].error
    )
    assert manual.status == "rejected" and manual.error is None


def test_selection_respects_daily_limit(db_session, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_clips_per_day", 4)
    monkeypatch.setattr(settings, "min_highlight_score", 10)
    s = factories.streamer(db_session, "kanal")
    for score in (90, 80, 70):
        _highlight(db_session, s, score, "session-1")
    select_group(db_session, settings, "session-1")
    for score in (95, 60):
        _highlight(db_session, s, score, "session-2")
    selected = select_group(db_session, settings, "session-2")
    assert [h.score for h in selected] == [95]  # nur noch 1 frei (4 - 3)
    assert "Tageslimit" in db_session.query(Highlight).filter_by(score=60).one().error


# --- Analyse echter Testvideos -------------------------------------------------
@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg nicht installiert")
def test_analysis_of_generated_samples(settings, database, db_session, monkeypatch):
    from app.services.clip_processing import ingest_samples
    from app.services.samples import generate_samples
    from app.workers.jobs.analysis import analyze_clip_job

    monkeypatch.setattr(settings, "test_mode", True)
    monkeypatch.setattr(settings, "min_highlight_score", 20)
    generate_samples(settings, count=2, duration=60)
    requests = ingest_samples(db_session, settings)
    db_session.commit()
    set_transcriber(FakeTranscriber())
    try:
        results = [analyze_clip_job(r.id) for r in requests]
    finally:
        set_transcriber(None)
    assert all("highlight" in r for r in results), results
    db_session.expire_all()
    highlights = db_session.query(Highlight).order_by(Highlight.id).all()
    assert len(highlights) == 2 and all(h.status == "candidate" for h in highlights)
    first = highlights[0]
    # Testvideo 1 hat laute Stellen bei 15 s und 42 s → beide im gewählten Fenster
    assert (
        first.start_seconds <= 15
        and first.end_seconds >= 45
        and first.end_seconds - first.start_seconds <= 45
    )
    assert 0 < first.score <= 100 and first.transcript == "Alter WAS! krass"
    assert first.analysis["words"][0]["start"] == pytest.approx(15.0 - first.start_seconds)
    assert len(first.perceptual_hash.split(",")) == 3
    assert db_session.get(ClipRequest, requests[0].id).analyzed_at is not None
    assert analyze_clip_job(requests[0].id) == {"skipped": "Status"}  # nicht doppelt analysieren


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg nicht installiert")
def test_same_vod_moment_is_duplicate(twitch_settings, database, db_session, make_user):
    from app.services.samples import generate_samples

    make_user()
    s = factories.authorized_streamer(db_session, twitch_settings)
    session = factories.stream_session(db_session, s)
    (sample,) = generate_samples(twitch_settings, count=1, duration=30)
    requests = []
    for i, end in enumerate((1030, 1040)):  # zwei Zuschauer-Clips vom selben Moment, 10 s versetzt
        copy = twitch_settings.downloads_dir / f"clip-{i}.mp4"
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(sample.read_bytes() + bytes([i]))
        requests.append(
            ClipRequest(
                streamer_id=s.id,
                stream_session_id=session.id,
                source="viewer_clip",
                dedup_key=f"viewer:{i}",
                vod_id="v-100",
                vod_offset=end,
                duration=30,
                status="downloaded",
                file_path=str(copy),
                sha256=f"{i}" * 64,
            )
        )
    db_session.add_all(requests)
    db_session.flush()
    first = analyze_clip_request(db_session, twitch_settings, requests[0], None)
    second = analyze_clip_request(db_session, twitch_settings, requests[1], None)
    assert first.status == "candidate"
    assert second.status == "duplicate" and f"#{first.id}" in second.error
