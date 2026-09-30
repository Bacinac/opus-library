#!/bin/bash
# The detector is public and fetched once into the volume. The embedder is not:
# AdaFace ships as PyTorch and what runs here is our own ONNX conversion, checked
# against the original (cosine 1.000000). It is staged onto the volume rather
# than baked into the image — 260 MB does not belong in git — and its absence is
# said plainly rather than worked around, because a face pass that quietly used
# a different model would produce vectors that mean something else.
set -euo pipefail
M="${FACES_MODELS:-/models}"
mkdir -p "$M"

if [ "$(stat -c%s "$M/detection.onnx" 2>/dev/null || echo 0)" -lt 10000000 ]; then
    echo "faces: fetching the detector"
    curl -fsSL --retry 3 -o "$M/detection.onnx" \
        "https://huggingface.co/immich-app/antelopev2/resolve/main/detection/model.onnx"
fi

# A hundred and six points around a face, where the detector gives five. Five
# are enough to lay two faces on top of each other and nowhere near enough to
# turn one INTO the other: a morph needs to know where the jaw and the brow and
# the lips are, or it slides one photograph over another.
if [ "$(stat -c%s "$M/mesh.onnx" 2>/dev/null || echo 0)" -lt 1000000 ]; then
    echo "faces: fetching the landmark mesh"
    curl -fsSL --retry 3 -o "$M/mesh.onnx" \
        "https://huggingface.co/public-data/insightface/resolve/main/models/buffalo_l/2d106det.onnx"
fi

if [ "$(stat -c%s "$M/embedding.onnx" 2>/dev/null || echo 0)" -lt 100000 ]; then
    cat >&2 <<'MISSING'
faces: the embedding model is not on the volume.

It is adaface_ir101_webface12m, converted from CVLface to ONNX. Put both parts
in the models volume — the data file must keep the name the graph refers to:

    embedding.onnx
    <the .data file under the exact name the graph refers to>

The graph names its external data file, so renaming that file silently detaches
the weights — the model then loads and answers with nothing in it. Check with:
    strings embedding.onnx | grep -o '[a-z_0-9]*\.onnx\.data'

Chosen by measurement over 4,320 of this library's own faces against 58 known
people; the comparison of nine encoders is in the project memory.
MISSING
    exit 1
fi

if [ "$(stat -c%s "$M/age.onnx" 2>/dev/null || echo 0)" -lt 100000 ]; then
    echo "faces: no age model on the volume — faces will be found without one" >&2
    echo "       (age.onnx plus its .data file, converted from a FairFace ViT)" >&2
fi

exec uvicorn app:app --host 0.0.0.0 --port 8099
