"""Hochformat-Layouts (1080×1920) aus einem Querformat-Stream.

- facecam_split: Facecam oben (erkannt in Phase 4), Gameplay darunter – typisches Streamer-Format
- blur_fill:     Gameplay mittig (leicht gezoomt), unscharfer Hintergrund, oben Platz für die Überschrift
- vertical:      Quelle ist bereits hochkant → einpassen, Ränder unscharf füllen
"""

from __future__ import annotations

from dataclasses import dataclass

OUT_W = 1080
OUT_H = 1920
CAM_MIN_H = 480
CAM_MAX_H = 760
BLUR_FG_H = 760  # Höhe des Gameplay-Bereichs bei blur_fill


@dataclass(frozen=True)
class Layout:
    name: str
    filter_graph: str  # liefert [v0] in 1080×1920
    subtitle_y: int  # vertikale Mitte der Untertitel
    title_y: int  # vertikale Mitte der Überschrift
    title_persistent: bool  # Überschrift die ganze Zeit (freie Fläche vorhanden) oder nur am Anfang


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def facecam_split(src_w: int, src_h: int, cam: dict) -> Layout:
    # Facecam-Ausschnitt im Seitenverhältnis 1080 : cam_h, zentriert auf die erkannte Box
    box_w = cam["w"] * src_w
    box_h = cam["h"] * src_h
    cam_h = _even(min(CAM_MAX_H, max(CAM_MIN_H, OUT_W * box_h / max(box_w, 1))))
    crop_w = min(src_w, max(box_w, box_h * OUT_W / cam_h))
    crop_h = min(src_h, crop_w * cam_h / OUT_W)
    crop_w = crop_h * OUT_W / cam_h
    center_x = (cam["x"] + cam["w"] / 2) * src_w
    center_y = (cam["y"] + cam["h"] / 2) * src_h
    cx = min(max(0, center_x - crop_w / 2), src_w - crop_w)
    cy = min(max(0, center_y - crop_h / 2), src_h - crop_h)

    # Gameplay: volle Höhe, Breite passend zum verbleibenden Bereich, möglichst ohne die Facecam
    game_h_out = OUT_H - cam_h
    game_crop_w = min(src_w, src_h * OUT_W / game_h_out)
    game_crop_h = min(src_h, game_crop_w * game_h_out / OUT_W)
    game_x = (src_w - game_crop_w) / 2
    cam_center_rel = center_x / src_w
    if cam_center_rel > 0.6:  # Facecam rechts → Gameplay-Ausschnitt etwas nach links
        game_x = max(0, game_x - 0.15 * game_crop_w)
    elif cam_center_rel < 0.4:
        game_x = min(src_w - game_crop_w, game_x + 0.15 * game_crop_w)
    game_y = (src_h - game_crop_h) / 2

    graph = (
        f"[0:v]split=2[camsrc][gamesrc];"
        f"[camsrc]crop={_even(crop_w)}:{_even(crop_h)}:{int(cx)}:{int(cy)},scale={OUT_W}:{cam_h},setsar=1[cam];"
        f"[gamesrc]crop={_even(game_crop_w)}:{_even(game_crop_h)}:{int(game_x)}:{int(game_y)},"
        f"scale={OUT_W}:{game_h_out},setsar=1[game];"
        f"[cam][game]vstack=inputs=2,format=yuv420p[v0]"
    )
    return Layout("facecam_split", graph, subtitle_y=cam_h + 150, title_y=110, title_persistent=False)


def blur_fill(src_w: int, src_h: int) -> Layout:
    fg_h = BLUR_FG_H
    scaled_w = _even(src_w * fg_h / src_h)
    fg_w = min(OUT_W, scaled_w)
    graph = (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
        f"gblur=sigma=28,eq=brightness=-0.12:saturation=1.1,setsar=1[bg];"
        f"[fgsrc]scale={scaled_w}:{fg_h},crop={fg_w}:{fg_h},setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v0]"
    )
    top_free = (OUT_H - fg_h) // 2
    return Layout(
        "blur_fill",
        graph,
        subtitle_y=OUT_H // 2 + fg_h // 2 + 170,
        title_y=top_free - 170,
        title_persistent=True,
    )


def vertical(src_w: int, src_h: int) -> Layout:
    graph = (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},gblur=sigma=28,setsar=1[bg];"
        f"[fgsrc]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v0]"
    )
    return Layout("vertical", graph, subtitle_y=int(OUT_H * 0.68), title_y=160, title_persistent=False)


def choose_layout(src_w: int, src_h: int, facecam: dict | None, *, prefer: str | None = None) -> Layout:
    if src_w <= 0 or src_h <= 0:
        raise ValueError("Unbekannte Videogröße")
    if src_h >= src_w:
        return vertical(src_w, src_h)
    if (
        prefer != "blur_fill"
        and facecam
        and facecam.get("confidence", 0) >= 0.3
        and facecam.get("w", 0) < 0.6
    ):
        return facecam_split(src_w, src_h, facecam)
    return blur_fill(src_w, src_h)
