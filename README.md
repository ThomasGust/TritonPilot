# Crab Detector v4 — DINOv2 AI Classification

## What's New

v4 replaces the hand-tuned colour thresholds with **DINOv2**, Meta's
pretrained vision transformer.  You feed it your 3 reference crab images
and it builds embedding vectors that capture fine visual details — carapace
shape, leg patterns, colour distribution.  At inference time each detected
blob is embedded and compared via cosine similarity against those references.

DINOv2 handles perspective distortion, compression, partial occlusion, and
lighting shifts far better than any colour heuristic because it learned
visual similarity from 142 million images.

The colour classifier is kept as an **automatic fallback** — if PyTorch
isn't installed or the model download fails, the detector still works
(just less reliably on degraded images).

## Architecture

```
Frame → Board quad finder → Perspective warp → Blob segmentation
                                                      ↓
                                              ┌─ DINOv2 (primary)
                                              │   Embed patch → cosine sim to 3 references
                                              │
                                              └─ Colour (fallback)
                                                  HSV saturation / warmth thresholds
                                                      ↓
                                              Map bboxes back → NMS → Display
```

## Installation

```bash
# Core (always needed)
pip install opencv-python numpy

# For DINOv2 classification (strongly recommended)
pip install torch torchvision transformers

# That's it — model weights (~84 MB) auto-download on first run.
```

### If you're on a GPU laptop (recommended for speed)
Install PyTorch with CUDA support following https://pytorch.org/get-started/locally/

### If you're CPU-only
Everything still works, just slower (~800ms per blob vs ~20ms on GPU).
For the competition (single frame, ~10 blobs), total time is ~8–10s on CPU
which is fine since you only trigger it once per attempt.


## Files to Copy

Extract over your `TritonPilot/TritonPilot/` directory:

```
tasks/crab_recognition/__init__.py
tasks/crab_recognition/crab_detector.py      ← main detector
tasks/crab_recognition/dino_classifier.py    ← NEW: DINOv2 embedding module
tools/crab_vision/__init__.py
tools/crab_vision/crab_detector.py           (re-export wrapper)
tools/crab_vision/run_on_samples.py          (test runner)
```

**No changes needed** to `gui/main_window.py` or any other files.
The API is fully backwards compatible.


## Quick Test

```bash
cd TritonPilot/TritonPilot

# Run on all 4 sample images with validation
python -m tools.crab_vision.run_on_samples --validate --show-all

# Run on a real camera screenshot
python -m tools.crab_vision.run_on_samples --image path/to/screenshot.png --show-all
```

First run will print:
```
Loading detector...
Loading DINOv2 ViT-S/14 from HuggingFace...
DINOv2 loaded on CPU  (or "on CUDA GPU" if you have one)
Detector ready in 12.3s — classification backend: DINOv2
```

Subsequent runs reuse the cached model and are much faster.

If you see `classification backend: colour heuristics`, it means PyTorch or
transformers isn't installed.  Install them and re-run.


## How It Works

**Board detection + perspective warp** (from v3):
The detector finds the white corrugated board as a quadrilateral, warps it
to a front-on 600×600 square, then segments dark blobs on the warped image.
If the board fills the entire frame (clean sample images), no warp is needed
and it processes directly.

**DINOv2 classification** (new in v4):
At startup, each reference image is embedded through DINOv2 ViT-S/14.
We also generate 8 augmented copies of each reference (random rotation,
crop, colour jitter) and average all embeddings to create a robust
class centroid.  This makes the classifier invariant to the exact
orientation and lighting of the reference.

At inference, each blob patch is cropped, padded to a square, embedded,
and compared against the 3 centroids.  The nearest-neighbor class wins.
The cosine similarity score (0–1) is returned as the detection confidence.


## Tuning

The main thing to tune is the **board detection**, not the classifier.
DINOv2 should just work once blobs are correctly segmented.

If the board isn't being found:
- Adjust `board_thresh_candidates` (default: 150, 160, 170, 180, 140, 190)
- Lower values for darker/underwater images

If too many background objects are detected as blobs:
- Increase `blob_min_area` (default: 800 pixels)
- Decrease `blob_max_area_frac` (default: 0.25)

DINOv2 similarity scores typically look like:
- Correct match: 0.6–0.85
- Wrong species: 0.3–0.5
- Random object: < 0.2
