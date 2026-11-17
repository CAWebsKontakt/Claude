from tests import factories
from tests.conftest import login


def test_video_requires_login_and_supports_range(client, make_user, db_session, settings):
    make_user("admin", "admin")
    s = factories.streamer(db_session, "kanal")
    h = factories.highlight(db_session, s)
    video = factories.rendered_video(
        db_session, h, settings.renders_dir / "v.mp4", settings.renders_dir / "v.jpg"
    )
    db_session.commit()

    assert client.get(f"/media/videos/{video.id}.mp4", follow_redirects=False).status_code == 303
    login(client)
    full = client.get(f"/media/videos/{video.id}.mp4")
    assert full.status_code == 200
    assert full.headers["content-type"] == "video/mp4"
    partial = client.get(f"/media/videos/{video.id}.mp4", headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206
    assert len(partial.content) == 100
    assert client.get(f"/media/thumbnails/{video.id}.jpg").status_code == 200
    download = client.get(f"/media/videos/{video.id}/download")
    assert "attachment" in download.headers["content-disposition"]


def test_files_outside_data_dir_are_never_served(client, make_user, db_session, settings, tmp_path):
    make_user("admin", "admin")
    s = factories.streamer(db_session, "kanal")
    h = factories.highlight(db_session, s)
    outside = tmp_path / "outside.mp4"
    video = factories.rendered_video(db_session, h, outside)
    db_session.commit()
    login(client)
    assert client.get(f"/media/videos/{video.id}.mp4").status_code == 404


def test_deleted_video_returns_404(client, make_user, db_session, settings):
    from app.timeutil import utcnow

    make_user("admin", "admin")
    s = factories.streamer(db_session, "kanal")
    h = factories.highlight(db_session, s)
    video = factories.rendered_video(db_session, h, settings.renders_dir / "gone.mp4")
    video.deleted_at = utcnow()
    db_session.commit()
    login(client)
    assert client.get(f"/media/videos/{video.id}.mp4").status_code == 404
