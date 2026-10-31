import numpy as np
import pytest

from clipit import facecam


def test_face_that_stays_put_wins_over_moving_game_faces():
    cam = [(i, 40 + i % 3 * 10, 125, 62, 62) for i in range(12)]
    game = [(i, 300 + i * 40, 200, 70, 70) for i in range(12)]
    ((face, frames),) = facecam.cam_faces(cam + game, 40)
    assert list(face) == [50, 125, 62, 62] and frames == set(range(12))


def test_each_scene_layout_is_found_separately():
    # Real case (Asmongold): the cam sits ~100 px lower in his react scene than in his game scene.
    # A loose clustering merged both into one position that matched neither.
    react = [(i, 114, 358, 83, 83) for i in range(17)]
    game = [(i, 79, 248, 90, 90) for i in range(20, 29)]
    faces = [list(f) for f, _ in facecam.cam_faces(react + game, 40)]
    assert faces == [[114, 358, 83, 83], [79, 248, 90, 90]]


def test_too_few_sightings_or_full_screen_camera_means_no_facecam():
    assert facecam.cam_faces([(i, 40, 125, 62, 62) for i in range(4)], 40) == []
    assert facecam.cam_faces([(i, 300, 100, 300, 300) for i in range(30)], 40) == []


def test_only_faces_against_a_frame_edge_count_as_webcams():
    assert facecam.at_edge((0.0, 0.62, 0.28, 0.35))  # bottom-left cam
    assert not facecam.at_edge((0.34, 0.26, 0.37, 0.46))  # a face in a video being reacted to, mid-screen


def test_each_clip_gets_the_layout_on_screen_during_it():
    layouts = [
        {"face": [114, 358, 83, 83], "box": [0, 0.62, 0.28, 0.35]},
        {"face": [79, 248, 90, 90], "box": [0, 0.44, 0.29, 0.36]},
    ]
    game_scene = [(0, 82, 250, 88, 88), (1, 75, 245, 92, 92), (2, 500, 100, 60, 60)]
    assert facecam.pick_layout(layouts, game_scene) == [0, 0.44, 0.29, 0.36]
    assert facecam.pick_layout(layouts, [(0, 82, 250, 88, 88)]) is None  # one sighting isn't enough
    assert facecam.pick_layout([], game_scene) is None


def test_no_frames_means_no_layout(monkeypatch):
    # If every frame grab for a clip fails, no layout may be picked unseen (it could crop the wrong region).
    layouts = [{"face": [114, 358, 83, 83], "box": [0, 0.62, 0.28, 0.35]}]
    assert facecam.pick_layout(layouts, []) is None
    monkeypatch.setattr(facecam, "grab_frames", lambda url, times: [])
    assert facecam.clip_cam("https://example/v.m3u8", layouts, 0, 30) is None


def test_box_snaps_to_overlay_border_despite_chat_and_room_clutter():
    # Mirrors CaseOh's real overlay: 0,68 to 202,218 at 960x540, chat text above it, a railing post inside it.
    rng = np.random.default_rng(0)
    frames = []
    for _ in range(40):
        f = np.full((540, 960), rng.uniform(60, 200), np.float32)
        for r in range(20, 60, 8):
            f[r, :150][rng.random(150) < 0.3] = 255
        f[68:218, 0:202] = rng.uniform(10, 50)
        f[100:218, 170:173] = 20
        frames.append(f.astype(np.uint8))
    x, y, w, h = facecam.cam_box(frames, (40, 125, 62, 62))
    want_h = 202 / (1080 / 768)  # trimmed to the top panel's shape
    assert abs(x * 960) < 1 and abs(w * 960 - 202) < 1
    assert abs(h * 540 - want_h) < 1 and abs(y * 540 - (68 + (150 - want_h) / 2)) < 1


def test_webcams_are_small_overlays_against_an_edge():
    assert facecam.webcam_like((0.0, 0.62, 0.29, 0.38))  # Asmongold's bottom-left cam
    assert facecam.webcam_like((0.40, 0.56, 0.21, 0.44))  # a cam centered along the bottom
    assert not facecam.webcam_like((0.34, 0.02, 0.37, 0.95))  # real case: a reacted video's panel, nearly full height
    assert not facecam.webcam_like((0.34, 0.26, 0.37, 0.46))  # mid-screen, touching no edge


def test_webcam_flush_with_the_frame_edge_is_kept(monkeypatch):
    # Review example: a 200x240 cam centered along the bottom edge. It has no border line at the bottom, and
    # trimming it to the panel shape pulls its crop ~50 px off that edge; the edge check must see the real overlay.
    rng = np.random.default_rng(1)
    frames = []
    for _ in range(12):
        f = np.full((540, 960), rng.uniform(60, 200), np.float32)
        f[300:540, 380:580] = rng.uniform(10, 50)
        frames.append(f.astype(np.uint8))
    face = (450, 380, 60, 60)
    x, y, w, h = facecam.overlay(frames, face)
    assert [round(v) for v in (x * 960, y * 540, w * 960, h * 540)] == [380, 300, 200, 240]
    assert not facecam.at_edge(facecam.cam_box(frames, face))  # the trimmed crop alone would fail the check

    monkeypatch.setattr(facecam, "grab_frames", lambda url, times: frames)
    monkeypatch.setattr(facecam, "detect_faces", lambda fr: [(i, *face) for i in range(len(fr))])
    (layout,) = facecam.find_layouts("https://example/v.m3u8", 3600)
    assert layout["face"] == list(face) and layout["box"] == list(facecam.cam_box(frames, face))


def test_no_border_falls_back_to_a_box_around_the_face():
    x, y, w, h = facecam.cam_box([np.full((540, 960), 100, np.uint8)] * 3, (400, 200, 60, 60))
    assert 0.1 < x < 0.5 and 0.2 < y < 0.5 and w < 0.3


def test_clip_face_crops_on_a_person_in_two_frames_and_ignores_small_faces(monkeypatch):
    monkeypatch.setattr(facecam, "grab_frames", lambda url, times: [])
    boxes = [(0, 480, 100, 100, 100), (1, 500, 100, 100, 100), (0, 10, 10, 30, 30), (2, 10, 10, 30, 30)]
    monkeypatch.setattr(facecam, "detect_faces", lambda frames: boxes)
    assert facecam.clip_face("u", 0, 30) == pytest.approx(0.5625, abs=0.02)
    monkeypatch.setattr(facecam, "detect_faces", lambda frames: boxes[1:])  # one big face, once: a passing cutscene
    assert facecam.clip_face("u", 0, 30) is None
