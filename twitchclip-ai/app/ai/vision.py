"""Bildanalyse mit OpenCV: Bewegung, Szenenwechsel, Facecam-Position, Wahrnehmungs-Hashes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

SAMPLE_FPS = 2.0
ANALYSIS_WIDTH = 320
FACE_WIDTH = 640


class VisionError(RuntimeError):
    pass


@dataclass
class VisionProfile:
    duration: float
    width: int
    height: int
    motion: list[float] = field(default_factory=list)  # je Sekunde 0..1
    scene_cuts: list[float] = field(default_factory=list)
    # Facecam als Anteil des Bildes {x, y, w, h} (0..1) oder None
    facecam: dict | None = None
    frame_hashes: list[str] = field(default_factory=list)  # dHash bei 25/50/75 %


def dhash(gray: np.ndarray) -> str:
    """64-Bit-Differenz-Hash (robust gegen Skalierung/Kompression)."""
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _face_detector():
    """Haar-Kaskade aus opencv-python (4.x). Fehlt sie, läuft die Analyse ohne Facecam-Erkennung."""
    if not hasattr(cv2, "CascadeClassifier"):
        return None
    path = (
        Path(getattr(cv2, "data", None).haarcascades if hasattr(cv2, "data") else "")
        / "haarcascade_frontalface_default.xml"
    )
    if not path.is_file():
        return None
    detector = cv2.CascadeClassifier(str(path))
    return None if detector.empty() else detector


def analyze_video(path: Path) -> VisionProfile:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise VisionError(f"Video nicht lesbar: {path.name}")
    try:
        fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        duration = total / fps if total else 0.0
        step = max(1, round(fps / SAMPLE_FPS))
        hash_frames = {max(0, int(total * q)) for q in (0.25, 0.5, 0.75)} if total else set()
        detector = _face_detector()

        motion_samples: list[tuple[float, float]] = []
        cuts: list[float] = []
        faces: list[tuple[float, float, float, float]] = []
        face_checks = 0
        hashes: list[str] = []
        prev_gray = None
        prev_hist = None
        index = 0
        sampled = 0
        while True:
            ok = capture.grab()
            if not ok:
                break
            wanted_hash = index in hash_frames
            if index % step == 0 or wanted_hash:
                ok, frame = capture.retrieve()
                if not ok:
                    break
                t = index / fps
                scale = ANALYSIS_WIDTH / frame.shape[1]
                small = cv2.resize(frame, (ANALYSIS_WIDTH, max(1, int(frame.shape[0] * scale))))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                if wanted_hash:
                    hashes.append(dhash(gray))
                if index % step == 0:
                    if prev_gray is not None:
                        motion_samples.append((t, float(np.mean(cv2.absdiff(gray, prev_gray))) / 255.0))
                    hist = cv2.calcHist([gray], [0], None, [32], [0, 256])
                    cv2.normalize(hist, hist)
                    if prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL) < 0.5:
                        cuts.append(round(t, 1))
                    prev_gray, prev_hist = gray, hist
                    if detector is not None and sampled % 4 == 0:
                        face_checks += 1
                        faces.extend(_detect_faces(detector, frame))
                    sampled += 1
            index += 1
        if duration == 0 and fps:
            duration = index / fps
    finally:
        capture.release()

    return VisionProfile(
        duration=round(duration, 2),
        width=width,
        height=height,
        motion=_motion_per_second(motion_samples, duration),
        scene_cuts=cuts,
        facecam=_facecam_region(faces, face_checks),
        frame_hashes=hashes,
    )


def _detect_faces(detector, frame: np.ndarray) -> list[tuple[float, float, float, float]]:
    scale = FACE_WIDTH / frame.shape[1]
    resized = cv2.resize(frame, (FACE_WIDTH, max(1, int(frame.shape[0] * scale))))
    gray = cv2.equalizeHist(cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY))
    h, w = gray.shape
    found = detector.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=6, minSize=(24, 24))
    return [(x / w, y / h, fw / w, fh / h) for (x, y, fw, fh) in found]


def _facecam_region(faces: list[tuple[float, float, float, float]], checks: int) -> dict | None:
    """Eine Facecam ist ein Gesicht, das in vielen Bildern an fast derselben Stelle steht."""
    if not faces or checks == 0:
        return None
    centers = np.array([(x + w / 2, y + h / 2) for x, y, w, h in faces])
    median = np.median(centers, axis=0)
    close = [f for f, c in zip(faces, centers, strict=True) if np.linalg.norm(c - median) < 0.08]
    if len(close) < max(2, 0.3 * checks):
        return None
    x, y, w, h = (float(np.median([f[i] for f in close])) for i in range(4))
    # Box um das Gesicht auf eine typische Webcam-Fläche erweitern
    cx, cy = x + w / 2, y + h / 2
    bw, bh = min(1.0, w * 3.2), min(1.0, h * 3.2)
    bx = min(max(0.0, cx - bw / 2), 1.0 - bw)
    by = min(max(0.0, cy - bh / 2), 1.0 - bh)
    return {"x": round(bx, 4), "y": round(by, 4), "w": round(bw, 4), "h": round(bh, 4),
            "confidence": round(len(close) / checks, 2)}  # fmt: skip


def _motion_per_second(samples: list[tuple[float, float]], duration: float) -> list[float]:
    seconds = max(1, int(np.ceil(duration)))
    values = np.zeros(seconds, dtype=np.float32)
    for t, value in samples:
        i = min(seconds - 1, int(t))
        values[i] = max(values[i], value)
    # 0.12 mittlere Pixeländerung ≈ sehr viel Bewegung
    return [round(float(v), 3) for v in np.clip(values / 0.12, 0.0, 1.0)]
