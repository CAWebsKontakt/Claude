"""Rendert alle Dashboard-Seiten mit Beispieldaten (Smoke-Test der Templates)."""

from app.models import ActivityLog, Highlight, PublishJob, TwitchClip
from tests import factories
from tests.conftest import login, post_form


def _seed(db, settings):
    s = factories.streamer(
        db,
        "livekanal",
        display_name="LiveKanal",
        is_live=True,
        live_title="Ranked",
        live_game_name="Valorant",
        live_viewer_count=1234,
    )
    h1 = factories.highlight(db, s, score=91, status="rendered", source_ref="h1")
    factories.highlight(db, s, score=45, status="candidate", source_ref="h2", title=None)
    factories.highlight(db, s, score=30, status="rejected", source_ref="h3")
    video = factories.rendered_video(db, h1, settings.renders_dir / "h1.mp4", settings.renders_dir / "h1.jpg")
    factories.publish_job(db, h1, video, status="pending_review")
    h4 = factories.highlight(db, s, score=88, status="published", source_ref="h4")
    factories.publish_job(db, h4, None, status="published")
    factories.twitch_clip(db, s)
    db.add(ActivityLog(level="error", category="twitch", message="Rate-Limit erreicht", streamer_id=s.id))
    db.commit()
    return s, h1, video


def test_all_pages_render(admin_client, db_session, settings):
    s, h1, _video = _seed(db_session, settings)
    pages = {
        "/": ["Übersicht", "LiveKanal", "Unglaublicher Moment"],
        "/streamers": ["LiveKanal", "Modus A"],
        f"/streamers/{s.id}": ["Rechte- und Autorisierungsstatus", "Neue Erlaubnis dokumentieren"],
        "/clips": ["Epischer Clip", "1234 Aufrufe"],
        "/highlights": ["Unglaublicher Moment", "Highlight #"],
        "/highlights?status=rejected": ["Verworfen"],
        "/highlights?min_score=80": ["Unglaublicher Moment"],
        f"/highlights/{h1.id}": ["Vorschau", "/media/videos/", "Transkript"],
        "/queue": ["Wartet auf Freigabe"],
        "/published": ["Veröffentlicht"],
        "/activity": ["Rate-Limit erreicht"],
        "/activity?level=error&category=twitch": ["Rate-Limit erreicht"],
        "/system": ["PostgreSQL", "Warteschlangen"],
        "/users": ["Neuen Benutzer anlegen", "admin"],
        "/account": ["Passwort ändern"],
    }
    for path, needles in pages.items():
        response = admin_client.get(path)
        assert response.status_code == 200, f"{path}: {response.status_code}"
        for needle in needles:
            assert needle in response.text, f"'{needle}' fehlt auf {path}"


def test_dashboard_mobile_meta_and_assets(admin_client):
    page = admin_client.get("/").text
    assert 'name="viewport"' in page
    assert "/static/css/app.css" in page
    assert admin_client.get("/static/css/app.css").status_code == 200
    assert admin_client.get("/static/js/app.js").status_code == 200


def test_reject_and_restore_highlight(admin_client, db_session, settings):
    _s, h1, _video = _seed(db_session, settings)
    post_form(admin_client, f"/highlights/{h1.id}/reject")
    db_session.expire_all()
    assert db_session.get(Highlight, h1.id).status == "rejected"
    post_form(admin_client, f"/highlights/{h1.id}/restore")
    db_session.expire_all()
    assert db_session.get(Highlight, h1.id).status == "rendered"


def test_cancel_queue_entry(admin_client, db_session, settings):
    _seed(db_session, settings)
    job = db_session.query(PublishJob).filter_by(status="pending_review").one()
    post_form(admin_client, f"/queue/{job.id}/cancel")
    db_session.expire_all()
    assert db_session.get(PublishJob, job.id).status == "cancelled"


def test_dismiss_clip(admin_client, db_session, settings):
    _seed(db_session, settings)
    clip = db_session.query(TwitchClip).one()
    post_form(admin_client, f"/clips/{clip.id}/dismiss")
    db_session.expire_all()
    assert db_session.get(TwitchClip, clip.id).status == "dismissed"
    assert "Epischer Clip" not in admin_client.get("/clips").text


def test_viewer_sees_no_edit_controls(client, make_user, db_session, settings):
    make_user("admin", "admin")
    make_user("viewer1", "viewer")
    s, _h1, _ = _seed(db_session, settings)
    login(client, "viewer1")
    page = client.get(f"/streamers/{s.id}").text
    assert "Neue Erlaubnis dokumentieren" not in page
    assert "Endgültig entfernen" not in page
    assert "Benutzer</a>" not in client.get("/").text


def test_404_page_is_html(admin_client):
    response = admin_client.get("/highlights/424242")
    assert response.status_code == 404
    assert "Nicht gefunden" in response.text
