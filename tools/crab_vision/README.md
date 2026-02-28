# Crab vision tool (standalone)

This folder contains a small computer-vision tool for **Task 2.1: Mitigate invasive species**
(European Green crab counting) from the 2026 MATE ROV Ranger manual.

Per the manual, teams can score full points for image recognition if their program:
- draws a bounding box around each **European Green crab**, and
- shows the total count on-screen.

## How it works
- Finds the white board region (largest bright area).
- Segments likely "crab ink" regions using a black-hat morphological filter.
- Classifies each candidate using ORB feature matching against the *three fixed reference images*
(green, rock, jonah).

## Run on sample images
From the `TritonPilot/TritonPilot` directory:

```bash
python -m tools.crab_vision.run_on_samples
```

Annotated outputs will be written to:
`data/img/crab/output/Crab Sample N_annotated.png`
