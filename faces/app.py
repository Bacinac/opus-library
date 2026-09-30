"""Faces: the one thing in this estate that needs a vendor's runtime.

It does inference and nothing else. It holds no database, decides nothing about
who anybody is, and keeps no state between calls — the library asks "what is in
this picture", it answers with boxes and vectors, and every judgement about
whose face that is stays where the rest of the judgement lives.

It reads the derivatives itself rather than being sent pictures. The preview is
already on disk, both containers can see it, and a 2048 px AVIF over HTTP would
be the same bytes moved twice for no reason. The library sends checksums.

Two models, and the reason for each is measured rather than assumed (the whole
comparison is in the project memory):

  detection   SCRFD-10G. A model three and a half times its size found one face
              FEWER over 3,104 photographs and framed them less precisely.
  embedding   AdaFace IR-101 trained on WebFace12M. Against the same crops it
              catches 87.9 % of same-person pairs where antelopev2 — which is
              what Immich runs — catches 84.5 %.
  age         A FairFace ViT, because the obvious choice cannot see children:
              InsightFace's own age model has a floor around twenty-five and
              called every seven-year-old in this library twenty-seven. This one
              puts them at nine, and that difference is the whole point — two
              sisters at the same age are told apart by nothing else.

Both run on the Arc. If the GPU is missing this refuses to start rather than
falling back to the processor: a silent fall to CPU would take a pass from
minutes to hours and hide that the card is broken.
"""

import base64
import os
import pathlib
import threading

import cv2
import numpy as np
import openvino as ov
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

MODELS = pathlib.Path(os.environ.get("FACES_MODELS", "/models"))
DERIVATIVES = pathlib.Path(os.environ.get("FACES_DERIVATIVES", "/derivatives"))
DEVICE = os.environ.get("FACES_DEVICE", "GPU")
BATCH = int(os.environ.get("FACES_BATCH", "32"))
DET_SIZE = 640
DET_THRESHOLD = float(os.environ.get("FACES_THRESHOLD", "0.5"))

# Midpoints of the FairFace age bins. The answer is the expectation over the
# whole softmax rather than the winning bin: one face is coarse either way, but
# a group is hundreds of faces and an expectation averages down where a label
# cannot.
AGE_BINS = np.array([1.0, 6.0, 15.0, 25.0, 35.0, 45.0, 55.0, 65.0, 80.0], np.float32)
AGE_PX = 224

# the ArcFace canonical template a 112x112 face is warped onto
TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)

app = FastAPI(title="OPUS faces")
state: dict = {}

# One request at a time into the models.
#
# A compiled model holds a single inference request, and OpenVINO refuses a
# second one while the first is running — "Infer Request is busy". It never came
# up while the passes were the only caller, because a pass is one client asking
# one thing at a time. A page of a hundred and twenty people asks for twenty runs
# at once, and every one of them failed.
#
# A lock rather than a queue of requests: the card is serial anyway, so this
# costs nothing and the alternative is an async queue for throughput that does
# not exist.
_card = threading.Lock()


@app.on_event("startup")
def load() -> None:
    core = ov.Core()
    if DEVICE not in core.available_devices:
        raise RuntimeError(
            f"{DEVICE} is not among {core.available_devices}. The Arc is passed to the "
            "LXC as /dev/dri/renderD129 — check the container has the device, and note "
            "renderD128 is the integrated graphics, not the card.")
    det = core.read_model(MODELS / "detection.onnx")
    emb = core.read_model(MODELS / "embedding.onnx")
    # Reshaped to a fixed batch before compiling. These graphs declare batch 1,
    # and handing OpenVINO a larger batch makes it recompile on every call —
    # measured at 1.5 faces a second against 480.
    emb.reshape({emb.inputs[0]: ov.PartialShape([BATCH, 3, 112, 112])})
    state["det"] = core.compile_model(det, DEVICE)
    state["emb"] = core.compile_model(emb, DEVICE)
    age_path = MODELS / "age.onnx"
    if age_path.exists():
        age = core.read_model(age_path)
        age.reshape({age.inputs[0]: ov.PartialShape([BATCH, 3, AGE_PX, AGE_PX])})
        state["age"] = core.compile_model(age, DEVICE)
    mesh_path = MODELS / "mesh.onnx"
    if mesh_path.exists():
        state["mesh"] = core.compile_model(core.read_model(mesh_path), DEVICE)
    state["device"] = core.get_property(DEVICE, "FULL_DEVICE_NAME")


class Ask(BaseModel):
    checksums: list[str]
    generation: int = 1


class AgeAsk(BaseModel):
    """Faces already found, asked only how old they look."""
    faces: list[dict]           # id, checksum, x, y, w, h
    generation: int = 1


def _detect(img: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Boxes and five landmarks, in the coordinates of the picture given."""
    h0, w0 = img.shape[:2]
    scale = DET_SIZE / max(h0, w0)
    small = cv2.resize(img, (int(round(w0 * scale)), int(round(h0 * scale))))
    canvas = np.zeros((DET_SIZE, DET_SIZE, 3), np.uint8)
    canvas[:small.shape[0], :small.shape[1]] = small
    blob = ((canvas[:, :, ::-1].astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]

    net = state["det"]
    with _card:
        out = net(blob)
    outs = [out[o] for o in net.outputs]

    boxes, kpss, scores = [], [], []
    for i, stride in enumerate((8, 16, 32)):
        sc = outs[i].reshape(-1)
        keep = sc >= DET_THRESHOLD
        if not keep.any():
            continue
        bb = outs[i + 3].reshape(-1, 4)[keep] * stride
        kp = outs[i + 6].reshape(-1, 10)[keep] * stride
        n = DET_SIZE // stride
        cx, cy = np.meshgrid(np.arange(n), np.arange(n))
        centres = np.repeat(np.stack([cx, cy], -1).reshape(-1, 2).astype(np.float32) * stride,
                            2, axis=0)[keep]
        boxes.append(np.stack([centres[:, 0] - bb[:, 0], centres[:, 1] - bb[:, 1],
                               centres[:, 0] + bb[:, 2], centres[:, 1] + bb[:, 3]], -1))
        kpss.append(kp.reshape(-1, 5, 2) + centres[:, None, :])
        scores.append(sc[keep])
    if not boxes:
        return np.zeros((0, 4)), np.zeros((0, 5, 2)), np.zeros(0)

    boxes = np.concatenate(boxes)
    kpss = np.concatenate(kpss)
    scores = np.concatenate(scores)
    # NMS can suppress everything it was given — overlapping weak candidates on
    # a patterned wall, say. It then returns nothing, and an empty numpy array is
    # float64, which cannot index anything: the whole batch of two dozen
    # photographs dies on one picture with no faces in it.
    kept = cv2.dnn.NMSBoxes(
        [[float(a), float(b), float(c - a), float(d - b)] for a, b, c, d in boxes],
        scores.tolist(), DET_THRESHOLD, 0.4)
    idx = np.asarray(kept, dtype=np.int64).reshape(-1)
    if idx.size == 0:
        return np.zeros((0, 4)), np.zeros((0, 5, 2)), np.zeros(0)
    return boxes[idx] / scale, kpss[idx] / scale, scores[idx]


def _align(img: np.ndarray, kps: np.ndarray) -> np.ndarray | None:
    M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), TEMPLATE, method=cv2.LMEDS)
    if M is None:
        return None
    return cv2.warpAffine(img, M, (112, 112), borderValue=0)


def _embed(crops: list[np.ndarray]) -> np.ndarray:
    net = state["emb"]
    out0 = net.outputs[0]
    vecs = np.zeros((len(crops), 512), np.float32)
    for s in range(0, len(crops), BATCH):
        chunk = crops[s:s + BATCH]
        batch = np.stack([((c[:, :, ::-1].astype(np.float32) - 127.5) / 127.5).transpose(2, 0, 1)
                          for c in chunk])
        if len(batch) < BATCH:
            batch = np.concatenate([batch, np.zeros((BATCH - len(batch), 3, 112, 112), np.float32)])
        with _card:
            vecs[s:s + len(chunk)] = net({0: batch})[out0][:len(chunk), :512]
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    return vecs


MESH_PX = 192

# Which of the hundred and six points are each eye. Taken from the model's own
# layout rather than guessed: the eyes decide the alignment, and an alignment
# built on the eyebrows drifts as somebody's brow changes with age.
EYE_LEFT = list(range(33, 43))
EYE_RIGHT = list(range(87, 97))

# where the eyes are put in the finished frame, as fractions of it
EYES = (0.35, 0.40, 0.65, 0.40)


def _mesh(img: np.ndarray, box) -> np.ndarray | None:
    """A hundred and six points around one face, in the picture's coordinates.

    The model wants the face cut out on its own terms: centred, and scaled so the
    longer side of the box fills two thirds of a 192 px square. The points come
    back in that square, so the same transform is inverted to put them back where
    the face actually is."""
    net = state.get("mesh")
    if net is None:
        return None
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    side = max(x2 - x1, y2 - y1) * 1.5
    if side <= 0:
        return None
    s = MESH_PX / side
    M = np.array([[s, 0, MESH_PX / 2 - s * cx],
                  [0, s, MESH_PX / 2 - s * cy]], np.float32)
    chip = cv2.warpAffine(img, M, (MESH_PX, MESH_PX), borderValue=0)
    blob = chip[:, :, ::-1].astype(np.float32).transpose(2, 0, 1)[None]
    with _card:
        out = net({0: blob})
    pred = np.asarray(list(out.values())[0]).reshape(-1, 2)
    pred = (pred + 1.0) * (MESH_PX / 2)
    back = cv2.invertAffineTransform(M)
    ones = np.ones((len(pred), 1), np.float32)
    return (np.hstack([pred.astype(np.float32), ones]) @ back.T).astype(np.float32)


def _shown(shape: np.ndarray, size: int) -> np.ndarray:
    """The outline of what a viewer actually sees of a face.

    Grown unevenly from the hull of the points, because they stop at the
    eyebrows: scaled evenly it keeps the chin and cuts the forehead off. Used
    both to mask the frame and to judge whether the frame is real — those have to
    be the same outline, or a frame is accepted on its face and shown with its
    edges, which is where the streaks were coming from."""
    hull = cv2.convexHull(np.float32(shape[:106]))
    middle = hull.mean(axis=0)
    away = hull - middle
    away[:, :, 0] *= 1.30
    away[:, :, 1] = np.where(away[:, :, 1] < 0, away[:, :, 1] * 1.85,
                             away[:, :, 1] * 1.18)
    mask = np.zeros((size, size), np.uint8)
    cv2.fillConvexPoly(mask, (middle + away).astype(np.int32), 255)
    return mask


def _warp(img: np.ndarray, src: np.ndarray, dst: np.ndarray,
          tris: np.ndarray, size: int) -> np.ndarray:
    """One picture pulled from the shape it has into the shape it should have.

    Triangle by triangle, because a face does not move as one piece: between two
    photographs of the same person the jaw travels and the eyes barely do, and a
    single transform for the whole frame is what makes a morph look like one
    photograph sliding over another."""
    out = np.zeros((size, size, 3), np.uint8)
    for a, b, c in tris:
        s = np.float32([src[a], src[b], src[c]])
        d = np.float32([dst[a], dst[b], dst[c]])
        x, y, w, h = cv2.boundingRect(d)
        # Held inside the canvas. A triangle touching the right or bottom edge
        # gives a rectangle one pixel past it, the slice comes back smaller than
        # the patch, and a triangle skipped rather than clipped is a wedge of
        # black over somebody's eye.
        x2, y2 = min(x + w, size), min(y + h, size)
        x, y = max(x, 0), max(y, 0)
        w, h = x2 - x, y2 - y
        if w <= 0 or h <= 0:
            continue
        # Sampled from the whole picture rather than from a cut-out of it: finding the
        # cut-out is where the edges go wrong, and warpAffine reads outside the frame on
        # its own terms.
        patch = cv2.warpAffine(
            img, cv2.getAffineTransform(s, d - np.float32([x, y])), (w, h),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        mask = np.zeros((h, w, 3), np.uint8)
        cv2.fillConvexPoly(mask, np.int32(d - np.float32([x, y])), (1, 1, 1), 16)
        area = out[y:y + h, x:x + w]
        area[:] = area * (1 - mask) + patch * mask
    return out


def _triangles(points: np.ndarray, size: int) -> np.ndarray:
    """How the points are joined up. Worked out once, on the average of all the
    shapes, so every frame is cut the same way — a triangulation computed per
    frame changes which points are neighbours and the mesh flickers."""
    inside = np.clip(points, 0, size - 1).astype(np.float32)
    sub = cv2.Subdiv2D((0, 0, size, size))
    for p in inside:
        sub.insert((float(p[0]), float(p[1])))
    tris = []
    for t in sub.getTriangleList():
        corners = t.reshape(3, 2)
        # Matched to the nearest point rather than looked up by its coordinates.
        # Subdiv2D hands back what it stored, not what was given it, and a lookup
        # that misses drops the whole triangle — which is a hole in every frame,
        # and a hole is drawn as black because the canvas starts black.
        got = []
        for corner in corners:
            near = int(np.argmin(np.abs(inside - corner).sum(axis=1)))
            if np.abs(inside[near] - corner).sum() <= 1.5:
                got.append(near)
        if len(got) == 3 and len(set(got)) == 3:
            tris.append(got)
    return np.asarray(tris, np.int32)


def _focus(crop: np.ndarray) -> float:
    """How much detail a face carries, measured at a fixed size.

    Judged after resizing rather than before, so a small distant face and a large
    blurred one are held to the same standard — which is right, because they are
    the same problem: there is nothing there to recognise. A face out of focus
    still embeds, and it embeds near every other face out of focus, so blur forms
    a person of its own out of everyone in the archive who was ever mis-focused.
    """
    small = cv2.resize(crop, (112, 112), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def _ages(crops: list[np.ndarray]) -> list[float | None]:
    """How old each face looks. Its own scaling: this model wants 0..1 centred,
    where the detector and the embedder each want something else — three models
    and three conventions, and mixing them up returns a plausible number rather
    than an error."""
    net = state.get("age")
    if net is None:
        return [None] * len(crops)
    out0 = net.outputs[0]
    ages: list[float | None] = []
    for s in range(0, len(crops), BATCH):
        chunk = crops[s:s + BATCH]
        batch = np.stack([
            (((cv2.resize(c, (AGE_PX, AGE_PX))[:, :, ::-1].astype(np.float32) / 255.0) - 0.5)
             / 0.5).transpose(2, 0, 1) for c in chunk])
        if len(batch) < BATCH:
            batch = np.concatenate([batch, np.zeros((BATCH - len(batch), 3, AGE_PX, AGE_PX),
                                                    np.float32)])
        with _card:
            logits = net(batch)[out0][:len(chunk)]
        e = np.exp(logits - logits.max(1, keepdims=True))
        p = e / e.sum(1, keepdims=True)
        ages.extend(float(row @ AGE_BINS) for row in p)
    return ages


_last: dict = {"key": None, "img": None}


def _read(checksum: str, generation: int) -> np.ndarray | None:
    """The preview, remembering only the last one.

    Faces arrive grouped by photograph and a photograph holds more than one, so
    a single slot removes most of the reading; a larger cache would hold whole
    images for no further gain."""
    key = (checksum, generation)
    if _last["key"] != key:
        _last["key"] = key
        _last["img"] = cv2.imread(str(_preview(checksum, generation)), cv2.IMREAD_COLOR)
    return _last["img"]


def _preview(checksum: str, generation: int) -> pathlib.Path:
    return DERIVATIVES / "preview" / checksum[:2] / f"{checksum}-g{generation:x}.avif"


@app.get("/health")
def health() -> dict:
    return {"ok": bool(state), "device": state.get("device", ""), "batch": BATCH,
            "age": "age" in state}


def _faces_in(img: np.ndarray) -> list[dict]:
    """Everything the models have to say about one picture."""
    h, w = img.shape[:2]
    boxes, kpss, scores = _detect(img)
    crops, keep = [], []
    for i in range(len(boxes)):
        crop = _align(img, kpss[i])
        if crop is not None:
            crops.append(crop)
            keep.append(i)
    vecs = _embed(crops) if crops else np.zeros((0, 512), np.float32)
    ages = _ages(crops) if crops else []
    faces = []
    for j, i in enumerate(keep):
        x1, y1, x2, y2 = boxes[i]
        faces.append({
            # fractions of the frame, so the same box is right against the
            # tile, the preview and the original
            "x": float(max(0.0, x1 / w)), "y": float(max(0.0, y1 / h)),
            "w": float(min(1.0, (x2 - x1) / w)), "h": float(min(1.0, (y2 - y1) / h)),
            "score": float(scores[i]),
            "embedding": vecs[j].tolist(),
            "apparent_age": ages[j] if j < len(ages) else None,
        })
    return faces


@app.post("/detect")
def detect(ask: Ask) -> dict:
    if not state:
        raise HTTPException(503, "models not loaded")
    out = []
    for checksum in ask.checksums:
        path = _preview(checksum, ask.generation)
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            # said rather than skipped: a preview that is not there is a fact the
            # caller has to record, not one for this service to swallow
            out.append({"checksum": checksum, "error": "no preview", "faces": []})
            continue
        try:
            faces = _faces_in(img)
        except Exception as exc:
            # a batch is two dozen photographs; one of them failing is one
            # photograph's problem and is reported as such
            out.append({"checksum": checksum, "error": f"detect: {exc}", "faces": []})
            continue
        out.append({"checksum": checksum, "faces": faces})
    return {"results": out}


class Frames(BaseModel):
    """Frames cut out of a recording, each with where in it they came from.

    Sent rather than read, and that is not a departure from the rule above it:
    that rule is about bytes already on disk, where a second copy over HTTP buys
    nothing. A sampled frame is on nobody's disk and never needs to be — it
    exists for the length of this question — so writing it down somewhere both
    containers can see would be inventing shared state to avoid a local copy."""
    frames: list[dict]          # at: seconds, jpeg: base64


@app.post("/detect-frames")
def detect_frames(ask: Frames) -> dict:
    if not state:
        raise HTTPException(503, "models not loaded")
    out = []
    for frame in ask.frames:
        at = float(frame.get("at", 0.0))
        raw = base64.b64decode(frame.get("jpeg") or "")
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            out.append({"at": at, "error": "not a picture", "faces": []})
            continue
        try:
            faces = _faces_in(img)
        except Exception as exc:
            out.append({"at": at, "error": f"detect: {exc}", "faces": []})
            continue
        out.append({"at": at, "faces": faces})
    return {"results": out}


class MorphAsk(BaseModel):
    """Faces already found and already chosen, asked to become one another."""
    faces: list[dict]           # id, year, checksum, x, y, w, h — several a year
    generation: int = 1
    size: int = 448
    steps: int = 10             # in-between frames per pair
    hold: int = 3               # frames a face is held before it starts changing
    ms: int = 60                # how long each frame is shown


@app.post("/morph")
def morph(ask: MorphAsk):
    """One face becoming the next, and the next, in order.

    Not a cross-fade. Between two photographs the geometry is carried across as
    well as the colour: the points of both faces are moved toward a shape halfway
    between them, both pictures are pulled into that shape, and only then are
    they mixed. A cross-fade of two different faces shows two faces at once and
    the eye reads it as one photograph sliding over another; carrying the shape
    is what makes it read as one face changing.

    Every year is asked for, and every year is answered if the archive can answer
    it. A year is handed a list of its faces rather than one of them, because
    what makes a photograph unusable here has nothing to do with what makes it a
    good photograph: a face at the very edge of the frame aligns into a window
    that reaches outside the picture, and there is nothing there to read. That is
    a reason to take another photograph from that year, not to lose the year —
    which is the whole point of a run through somebody's life.
    """
    size = max(96, min(512, ask.size))
    by_year: dict[int, list] = {}
    for f in ask.faces:
        by_year.setdefault(int(f.get("year", 0)), []).append(f)

    def prepare(f: dict) -> dict | None:
        return _morph_frame(f, ask.generation, size)

    chosen, spare = _first_usable(by_year, prepare)
    if len(chosen) < 2:
        raise HTTPException(422, "two faces at least are needed to become one another")
    if len(chosen) > 3:
        _second_look(chosen, spare, prepare)

    years = sorted(chosen)
    shapes = [chosen[y]["shape"] for y in years]
    images = _evened([chosen[y]["image"] for y in years], shapes)
    frames = _morph_frames(images, shapes, size, ask.steps, ask.hold)
    # said out loud, because a year quietly missing from somebody's life is a
    # question that would otherwise have no answer
    return Response(content=_webp(frames, ask.ms), media_type="image/webp",
                    headers={"X-OPUS-Frames": f"{len(years)}/{len(by_year)}",
                             "X-OPUS-Years": ",".join(str(y) for y in years)})


def _morph_frame(f: dict, generation: int, size: int) -> dict | None:
    """One candidate, turned into a frame — or nothing, with the reason kept
    to itself. Everything that can disqualify a photograph is decided here,
    so the caller only has to ask for the next one."""
    img = _read(f["checksum"], generation)
    if img is None:
        return None
    h, w = img.shape[:2]
    box = _found_again(img, (f["x"] * w, f["y"] * h,
                             (f["x"] + f["w"]) * w, (f["y"] + f["h"]) * h))
    if box is None:
        return None
    pts = _mesh(img, box)
    if pts is None:
        raise HTTPException(503, "no landmark mesh on the volume")

    tl = np.float32([EYES[0] * size, EYES[1] * size])
    tr = np.float32([EYES[2] * size, EYES[3] * size])
    left, right = pts[EYE_LEFT].mean(axis=0), pts[EYE_RIGHT].mean(axis=0)
    have = np.linalg.norm(right - left)
    if have <= 0:
        return None
    scale = np.linalg.norm(tr - tl) / have
    angle = np.arctan2(*(tr - tl)[::-1]) - np.arctan2(*(right - left)[::-1])
    cos, sin = scale * np.cos(angle), scale * np.sin(angle)
    M = np.array([[cos, -sin, tl[0] - (cos * left[0] - sin * left[1])],
                  [sin, cos, tl[1] - (sin * left[0] + cos * left[1])]], np.float32)

    # how much of the window is really in the photograph. A face near the
    # edge aligns into a window reaching past it, and the border smears the
    # last row of pixels sideways — vertical streaks the morph then bends
    # around the face, which look exactly like a tear
    seen = cv2.warpAffine(np.full((h, w), 255, np.uint8), M, (size, size),
                          flags=cv2.INTER_NEAREST,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    ones = np.ones((len(pts), 1), np.float32)
    shape = (np.hstack([pts, ones]) @ M.T).astype(np.float32)
    inside = _shown(shape, size) > 0
    if not inside.any() or (seen[inside] > 0).mean() < 0.995:
        return None

    frame = cv2.warpAffine(img, M, (size, size), flags=cv2.INTER_CUBIC,
                           borderMode=cv2.BORDER_REPLICATE)
    return {"image": frame, "shape": shape,
            "lit": float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[inside].mean())}


def _found_again(img: np.ndarray, want: tuple) -> np.ndarray | None:
    """The detection that is the stored box. Found again rather than trusted:
    the stored box says where a face is, and a mesh fitted to a loose one puts
    its points on nothing."""
    box, best = None, 0.0
    for found, _pts in zip(*_detect(img)[:2]):
        x1, y1 = max(want[0], found[0]), max(want[1], found[1])
        x2, y2 = min(want[2], found[2]), min(want[3], found[3])
        inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        union = ((want[2] - want[0]) * (want[3] - want[1])
                 + (found[2] - found[0]) * (found[3] - found[1]) - inter)
        iou = inter / union if union > 0 else 0.0
        if iou > best:
            best, box = iou, found
    return box if best >= 0.5 else None


def _first_usable(by_year: dict[int, list], prepare) -> tuple[dict, dict]:
    """Each year's first photograph that makes a frame, and the ones after it
    kept in case it turns out to be the wrong one."""
    chosen, spare = {}, {}
    for year in sorted(by_year):
        for i, f in enumerate(by_year[year]):
            got = prepare(f)
            if got:
                chosen[year] = got
                spare[year] = by_year[year][i + 1:]
                break
    return chosen, spare


def _second_look(chosen: dict, spare: dict, prepare) -> None:
    """A second look at the years that stand out. The first usable photograph of
    a year is not always the right one — a mesh can land on a face that is turned
    away, or the light can have gone — and both show up as a frame unlike its
    neighbours. Every one of those is offered the rest of its year before it is
    allowed to stay."""
    for _round in range(2):
        run = _run_middle(chosen)
        swapped = 0
        for year in sorted(chosen):
            score = _odd(chosen[year], *run)
            if score <= 3.0:
                continue
            for i, f in enumerate(spare.get(year, [])):
                alt = prepare(f)
                if alt is not None and _odd(alt, *run) < score:
                    chosen[year] = alt
                    spare[year] = spare[year][i + 1:]
                    swapped += 1
                    break
        if not swapped:
            return


def _run_middle(chosen: dict) -> tuple:
    years = sorted(chosen)
    shapes = np.stack([chosen[y]["shape"] for y in years])
    lits = np.array([chosen[y]["lit"] for y in years])
    shape_mid, lit_mid = np.median(shapes, axis=0), float(np.median(lits))
    shape_spread = float(np.median([np.abs(sh - shape_mid).mean() for sh in shapes])) or 1.0
    lit_spread = float(np.median(np.abs(lits - lit_mid))) or 1.0
    return shape_mid, lit_mid, shape_spread, lit_spread


def _odd(got: dict, shape_mid, lit_mid, shape_spread, lit_spread) -> float:
    return max(np.abs(got["shape"] - shape_mid).mean() / shape_spread,
               abs(got["lit"] - lit_mid) / lit_spread)


COLOUR = 0.55


def _evened(images: list[np.ndarray], shapes: list[np.ndarray]) -> list[np.ndarray]:
    """Every frame brought to one exposure and one cast. Twenty years of
    different cameras in different light, and the jump from one to the next is
    what the eye notices first — far more than the face changing. Each is
    measured inside its own face and moved toward what the run does on average:
    the lightness matched fully, because exposure carries no information about a
    person, and the colour only part of the way, because a tan in July is real."""
    measured = [_tone(im, sh) for im, sh in zip(images, shapes)]
    want_mean = np.median(np.stack([m for _, m, _ in measured]), axis=0)
    want_std = np.median(np.stack([d for _, _, d in measured]), axis=0)
    pull = np.array([1.0, COLOUR, COLOUR], np.float32)
    evened = []
    for lab, mean, std in measured:
        m = mean + (want_mean - mean) * pull
        d = std + (want_std - std) * pull
        out = (lab - mean) * (d / std) + m
        evened.append(cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR))
    return evened


def _tone(frame: np.ndarray, shape: np.ndarray) -> tuple:
    hull = cv2.convexHull(np.float32(shape)).astype(np.int32)
    inside = np.zeros(frame.shape[:2], np.uint8)
    cv2.fillConvexPoly(inside, hull, 255)
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB).astype(np.float32)
    mean, std = cv2.meanStdDev(lab, mask=inside)
    return lab, mean.reshape(3), np.maximum(std.reshape(3), 1e-3)


def _morph_frames(images: list[np.ndarray], shapes: list[np.ndarray], size: int,
                  steps: int, hold: int) -> list[np.ndarray]:
    # the corners and the edge midpoints, so the background is carried too and
    # the warp does not tear away from the frame
    edge = np.float32([[0, 0], [size / 2, 0], [size - 1, 0], [0, size / 2],
                       [size - 1, size / 2], [0, size - 1], [size / 2, size - 1],
                       [size - 1, size - 1]])
    shapes = [np.clip(np.vstack([sh, edge]), 0, size - 1) for sh in shapes]
    tris = _triangles(np.mean(shapes, axis=0), size)

    frames = []
    for i in range(len(images) - 1):
        a, b = shapes[i], shapes[i + 1]
        for _ in range(hold):
            frames.append(_masked(images[i], a, size) if i == 0 else frames[-1])
        for k in range(1, steps + 1):
            raw = k / (steps + 1)
            # the shape travels smoothly and the colour crosses over sharply.
            # Halfway through, a morph is honestly two faces at once and looks
            # soft; the shape has to move through that point evenly or the head
            # lurches, but the picture does not have to LINGER there
            t = raw * raw * (3 - 2 * raw)
            alpha = t ** 3 / (t ** 3 + (1 - t) ** 3)
            between = (1 - t) * a + t * b
            wa = _warp(images[i], a, between, tris, size)
            wb = _warp(images[i + 1], b, between, tris, size)
            frames.append(_masked(cv2.addWeighted(wa, 1 - alpha, wb, alpha, 0), between, size))
        frames.append(_masked(images[i + 1], shapes[i + 1], size))
    for _ in range(hold):
        frames.append(frames[-1])
    return frames


def _masked(frame: np.ndarray, shape: np.ndarray, size: int) -> np.ndarray:
    """Only the face is shown. Two photographs taken years apart have nothing in
    common behind the head, so a morph of the whole frame ghosts one room over
    another — and it is the background, not the face, that makes it look like a
    trick. The mask follows the shape being morphed, so it hugs the face as it
    changes rather than breathing against it."""
    mask = cv2.GaussianBlur(_shown(shape, size), (0, 0), size * 0.022)
    soft = (mask.astype(np.float32) / 255.0)[:, :, None]
    ground = np.full_like(frame, 24)
    return (frame * soft + ground * (1 - soft)).astype(np.uint8)


def _webp(frames: list[np.ndarray], ms: int) -> bytes:
    from io import BytesIO
    from PIL import Image

    out = BytesIO()
    Image.fromarray(frames[0][:, :, ::-1]).save(
        out, format="WEBP", save_all=True, quality=80, method=4,
        append_images=[Image.fromarray(f[:, :, ::-1]) for f in frames[1:]],
        duration=ms, loop=0)
    return out.getvalue()


@app.post("/landmarks")
def landmarks(ask: AgeAsk) -> dict:
    """The five points on each of these faces: both eyes, the nose, the corners
    of the mouth.

    Worked out by looking again rather than remembered, because they were thrown
    away when the faces were found. Detection is run on the whole preview and the
    answer matched back to the box we already hold — the same face, found the
    same way, so the match is a formality and a miss means the box came from a
    picture that has since changed.

    They are what lets one face be laid over another. A box says where a face is;
    only the points say which way it is turned."""
    out = []
    for f in ask.faces:
        img = _read(f["checksum"], ask.generation)
        if img is None:
            out.append({"id": f["id"], "landmarks": None})
            continue
        h, w = img.shape[:2]
        want = (f["x"] * w, f["y"] * h, (f["x"] + f["w"]) * w, (f["y"] + f["h"]) * h)
        boxes, kps, _scores = _detect(img)
        best, hit = 0.0, None
        for box, points in zip(boxes, kps):
            x1, y1 = max(want[0], box[0]), max(want[1], box[1])
            x2, y2 = min(want[2], box[2]), min(want[3], box[3])
            inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
            union = ((want[2] - want[0]) * (want[3] - want[1])
                     + (box[2] - box[0]) * (box[3] - box[1]) - inter)
            iou = inter / union if union > 0 else 0.0
            if iou > best:
                best, hit = iou, points
        if hit is None or best < 0.5:
            out.append({"id": f["id"], "landmarks": None})
            continue
        # as fractions of the frame, like the box, so they stay true against the
        # tile, the preview and the original alike. Reshaped rather than assumed:
        # the detector hands its points back flat here and paired elsewhere
        pts = np.asarray(hit, dtype=np.float32).reshape(-1, 2)
        out.append({"id": f["id"],
                    "landmarks": [float(v) for pt in pts for v in (pt[0] / w, pt[1] / h)]})
    return {"results": out}


@app.post("/focus")
def focus(ask: AgeAsk) -> dict:
    """How sharp these already-found faces are.

    The same crop the age model is given, for the same reason: the question is
    about the face as a person would see it, hair and chin included, not about
    the landmark box."""
    out = []
    for f in ask.faces:
        img = _read(f["checksum"], ask.generation)
        if img is None:
            out.append({"id": f["id"], "focus": None})
            continue
        h, w = img.shape[:2]
        cx, cy = (f["x"] + f["w"] / 2) * w, (f["y"] + f["h"] / 2) * h
        side = max(f["w"] * w, f["h"] * h) * 1.45
        left = int(max(0, min(w - 1, cx - side / 2)))
        top = int(max(0, min(h - 1, cy - side / 2)))
        side = int(min(side, w - left, h - top))
        if side < 8:
            out.append({"id": f["id"], "focus": 0.0})
            continue
        out.append({"id": f["id"], "focus": _focus(img[top:top + side, left:left + side])})
    return {"results": out}


@app.post("/age")
def age(ask: AgeAsk) -> dict:
    """How old these already-found faces look.

    Cut from the box that is already stored rather than re-detected and
    re-aligned: this model reads a face, not a landmark, so it needs neither —
    and re-running detection to answer a question detection does not ask would
    cost two hours and throw away every grouping on the way.

    The same 45 % of room around the box that a person is shown, because that is
    what the model was trained on: faces, with their hair and their chins."""
    if "age" not in state:
        raise HTTPException(503, "no age model")
    out, batch, meta = [], [], []

    def flush():
        if not batch:
            return
        ages = _ages(batch)
        for m, a in zip(meta, ages):
            out.append({"id": m, "apparent_age": a})
        batch.clear()
        meta.clear()

    for f in ask.faces:
        img = _read(f["checksum"], ask.generation)
        if img is None:
            out.append({"id": f["id"], "apparent_age": None})
            continue
        h, w = img.shape[:2]
        cx, cy = (f["x"] + f["w"] / 2) * w, (f["y"] + f["h"] / 2) * h
        side = max(f["w"] * w, f["h"] * h) * 1.45
        left = int(max(0, min(w - 1, cx - side / 2)))
        top = int(max(0, min(h - 1, cy - side / 2)))
        side = int(min(side, w - left, h - top))
        if side < 8:
            out.append({"id": f["id"], "apparent_age": None})
            continue
        batch.append(img[top:top + side, left:left + side])
        meta.append(f["id"])
        if len(batch) >= BATCH:
            flush()
    flush()
    return {"results": out}
