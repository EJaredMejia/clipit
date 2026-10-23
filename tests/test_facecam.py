import numpy as np

from clipit import facecam


def test_face_that_stays_put_wins_over_moving_game_faces():
    cam = [(i, 40 + i % 3 * 10, 125, 62, 62) for i in range(12)]
    game = [(i, 300 + i * 40, 200, 70, 70) for i in range(12)]
    face = facecam.cam_face(cam + game, 40)
    assert face is not None and list(face) == [50, 125, 62, 62]


def test_too_few_sightings_or_full_screen_camera_means_no_facecam():
    cam = [(i, 40, 125, 62, 62) for i in range(5)]
    assert facecam.cam_face(cam, 40) is None
    assert facecam.cam_face([(i, 300, 100, 300, 300) for i in range(30)], 40) is None


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


def test_no_border_falls_back_to_a_box_around_the_face():
    x, y, w, h = facecam.cam_box([np.full((540, 960), 100, np.uint8)] * 3, (400, 200, 60, 60))
    assert 0.1 < x < 0.5 and 0.2 < y < 0.5 and w < 0.3
