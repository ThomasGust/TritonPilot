import json
from pathlib import Path

import cv2
import numpy as np
from pytest import approx

from tools.realityscan_underwater_pipeline import (
    MISSING_REALITYSCAN_OUTPUT_EXIT_CODE,
    VariantSpec,
    apply_reconstruction_preset,
    build_arg_parser,
    build_variant_specs,
    filter_obj_large_faces,
    final_reconstruction_exit_code,
    load_stereo_calibration,
    load_stereo_session,
    make_geometry_frame,
    prepare_output_paths,
    read_stereo_session_metrics,
    scale_model_from_stereo_baseline,
    selected_image_prior_commands,
    write_realityscan_command_file,
    write_stereo_variant_frames,
)


def _write_image(path: Path, offset: int) -> None:
    y, x = np.indices((12, 16))
    image = np.dstack(
        [
            (x * 12 + offset) % 255,
            (y * 18 + offset) % 255,
            ((x + y) * 9 + offset) % 255,
        ]
    ).astype(np.uint8)
    assert cv2.imwrite(str(path), image)


def _make_stereo_session(tmp_path: Path) -> Path:
    session_dir = tmp_path / "stereo_sessions" / "test-session"
    (session_dir / "left").mkdir(parents=True)
    (session_dir / "right").mkdir()
    frames = []
    for index in range(1, 3):
        stem = f"pair_{index:06d}"
        left = session_dir / "left" / f"{stem}_left.png"
        right = session_dir / "right" / f"{stem}_right.png"
        _write_image(left, index)
        _write_image(right, index + 20)
        frames.append(
            {
                "index": index,
                "stem": stem,
                "left_path": f"left\\{stem}_left.png",
                "right_path": f"right\\{stem}_right.png",
                "pair_delta_ms": 1.5,
                "left": {"wall_ts": 100.0 + index, "shape": [12, 16, 3]},
                "right": {"wall_ts": 100.0015 + index, "shape": [12, 16, 3]},
            }
        )
    (session_dir / "manifest.json").write_text(json.dumps({"frames": frames}), encoding="utf-8")
    return session_dir


def _make_calibration(tmp_path: Path) -> Path:
    calibration = {
        "image_size": [16, 12],
        "rig_id": "unit_test_rig",
        "left": {
            "camera_matrix": [[10.0, 0.0, 8.0], [0.0, 11.0, 6.0], [0.0, 0.0, 1.0]],
            "dist_coeffs": [[0.1, 0.01, 0.001, 0.002, 0.0001]],
        },
        "right": {
            "camera_matrix": [[10.5, 0.0, 7.5], [0.0, 10.8, 6.5], [0.0, 0.0, 1.0]],
            "dist_coeffs": [[0.2, 0.02, 0.003, 0.004, 0.0002]],
        },
        "stereo": {
            "baseline": 100.0,
            "rotation": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "translation": [-100.0, 0.0, 0.0],
        },
    }
    path = tmp_path / "stereo_calibration.json"
    path.write_text(json.dumps(calibration), encoding="utf-8")
    return path


def test_load_stereo_session_scores_pairs(tmp_path: Path):
    session = load_stereo_session(_make_stereo_session(tmp_path))

    info, metrics = read_stereo_session_metrics(session, max_pair_delta_ms=75.0)

    assert info.frame_count == 2
    assert info.width == 16
    assert info.height == 12
    assert [metric.source_stem for metric in metrics] == ["pair_000001", "pair_000002"]
    assert metrics[0].pair_delta_ms == approx(1.5)


def test_write_stereo_frames_and_realityscan_xmp(tmp_path: Path):
    session = load_stereo_session(_make_stereo_session(tmp_path))
    calibration = load_stereo_calibration(_make_calibration(tmp_path))
    _, selected = read_stereo_session_metrics(session, max_pair_delta_ms=75.0)
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()

    result = write_stereo_variant_frames(
        session,
        selected,
        frames_dir,
        VariantSpec(name="enhanced_brown4", geometry_mode="enhanced"),
        crop_fraction=0.0,
        wb_gain=2.0,
        clahe_clip=2.0,
        sharpen=0.0,
        jpeg_quality=95,
        texture_layers=False,
        calibration=calibration,
        distortion_model="Brown4WithTangential2",
        xmp_pose_prior="exact",
        xmp_calibration_prior="exact",
        translation_scale=0.001,
        include_rig_priors=False,
    )

    assert len(result.image_paths) == 4
    assert len(result.contact_paths) == 2
    right_xmp = frames_dir / "pair_000001_right_t_0000.000.xmp"
    text = right_xmp.read_text(encoding="utf-8")
    assert 'xcr:CalibrationPrior="exact"' in text
    assert 'xcr:CalibrationGroup="2"' in text
    assert 'xcr:DistortionModel="brown4t2"' in text
    assert "xcr:Rig" not in text
    assert "xcr:RigPoseIndex" not in text
    assert "xcr:Position" not in text


def test_stereo_xmp_mode_does_not_clobber_camera_groups():
    class Args:
        using_stereo_xmp_priors = True

    commands = selected_image_prior_commands(Args())

    assert commands == ["-setFeatureSource 2"]


def test_missing_realityscan_model_is_failure(tmp_path: Path):
    missing_model = tmp_path / "missing.obj"

    assert final_reconstruction_exit_code(0, missing_model) == MISSING_REALITYSCAN_OUTPUT_EXIT_CODE


def _write_solved_xmp(path: Path, position: tuple[float, float, float]) -> None:
    path.write_text(
        f"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:xcr="http://www.capturingreality.com/ns/xcr/1.1#">
      <xcr:Position>{position[0]} {position[1]} {position[2]}</xcr:Position>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
""",
        encoding="utf-8",
    )


def test_metric_scale_from_solved_stereo_xmp_writes_meter_obj(tmp_path: Path):
    calibration = load_stereo_calibration(_make_calibration(tmp_path))
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for index, x_offset in enumerate((0.0, 5.0, 10.0), start=1):
        stem = f"pair_{index:06d}"
        _write_solved_xmp(frames_dir / f"{stem}_left_t_0000.000.xmp", (x_offset, 0.0, 0.0))
        _write_solved_xmp(frames_dir / f"{stem}_right_t_0000.000.xmp", (x_offset + 2.0, 0.0, 0.0))

    model = tmp_path / "model.obj"
    model.write_text(
        "mtllib model.mtl\n"
        "v 1.0 2.0 3.0\n"
        "v -2.0 0.0 4.0 0.4 0.5 0.6\n"
        "vn 0.0 0.0 1.0\n"
        "f 1 2 1\n",
        encoding="utf-8",
    )

    result = scale_model_from_stereo_baseline(
        model,
        frames_dir,
        calibration,
        translation_scale=0.001,
        min_pairs=3,
        report_path=tmp_path / "metric_scale.json",
    )

    assert result.real_baseline_m == approx(0.1)
    assert result.reconstructed_baseline_units == approx(2.0)
    assert result.scale_factor == approx(0.05)
    text = result.metric_model.read_text(encoding="utf-8")
    assert "v 0.05 0.1 0.15" in text
    assert "v -0.1 0 0.2 0.4 0.5 0.6" in text
    assert "vn 0.0 0.0 1.0" in text


def test_final_command_exports_solved_xmp_for_metric_scaling(tmp_path: Path):
    class Args:
        detector_sensitivity = "Ultra"
        distortion_model = "Brown4WithTangential2"
        images_overlap = "Low"
        max_features_per_image = 80000
        max_features_per_mpx = 20000
        metric_scale_active = True
        model_quality = "preview"
        normal_downscale = 2
        preselector_features = 20000
        recon_region_scale_xy = 1.25
        recon_region_scale_z = 1.35
        simplify_triangles = 0
        texture_count = 1
        texture_resolution = 512
        try_merge_components = False
        using_stereo_xmp_priors = False

    paths = prepare_output_paths(tmp_path / "session", tmp_path / "out", overwrite=False)

    write_realityscan_command_file(paths, Args(), frames_dir=tmp_path / "frames")

    text = paths.rscmd.read_text(encoding="utf-8")
    assert "-exportXMPForSelectedComponent" in text
    assert text.index("-exportModel") < text.index("-exportXMPForSelectedComponent")


def test_final_command_skips_realityscan_clean_model_by_default(tmp_path: Path):
    class Args:
        detector_sensitivity = "Ultra"
        distortion_model = "Brown4WithTangential2"
        images_overlap = "Low"
        max_features_per_image = 80000
        max_features_per_mpx = 20000
        metric_scale_active = False
        model_quality = "preview"
        normal_downscale = 2
        preselector_features = 20000
        recon_region_scale_xy = 1.25
        recon_region_scale_z = 1.35
        simplify_triangles = 0
        texture_count = 1
        texture_resolution = 512
        try_merge_components = False
        using_stereo_xmp_priors = False

    paths = prepare_output_paths(tmp_path / "session", tmp_path / "out", overwrite=False)

    write_realityscan_command_file(paths, Args(), frames_dir=tmp_path / "frames")

    text = paths.rscmd.read_text(encoding="utf-8")
    assert "-cleanModel" not in text
    assert '-exportModel "Model 1"' in text


def test_final_command_can_still_use_realityscan_clean_model(tmp_path: Path):
    class Args:
        clean_model = True
        detector_sensitivity = "Ultra"
        distortion_model = "Brown4WithTangential2"
        images_overlap = "Low"
        max_features_per_image = 80000
        max_features_per_mpx = 20000
        metric_scale_active = False
        model_quality = "preview"
        normal_downscale = 2
        preselector_features = 20000
        recon_region_scale_xy = 1.25
        recon_region_scale_z = 1.35
        simplify_triangles = 0
        texture_count = 1
        texture_resolution = 512
        try_merge_components = False
        using_stereo_xmp_priors = False

    paths = prepare_output_paths(tmp_path / "session", tmp_path / "out", overwrite=False)

    write_realityscan_command_file(paths, Args(), frames_dir=tmp_path / "frames")

    text = paths.rscmd.read_text(encoding="utf-8")
    assert "-cleanModel" in text
    assert '-exportModel "Model 2"' in text


def test_large_face_filter_removes_broad_infill_triangles(tmp_path: Path):
    model = tmp_path / "model.obj"
    model.write_text(
        "mtllib model.mtl\n"
        "v 0 0 0\n"
        "v 1 0 0\n"
        "v 0 1 0\n"
        "v 100 0 0\n"
        "v 0 100 0\n"
        "usemtl surface\n"
        "f 1 2 3\n"
        "f 1 2 3\n"
        "f 1 2 3\n"
        "f 1 2 3\n"
        "f 1 2 3\n"
        "f 1 4 5\n",
        encoding="utf-8",
    )

    result = filter_obj_large_faces(
        model,
        area_ratio=20.0,
        min_faces=1,
        report_path=tmp_path / "filter.json",
    )

    text = model.read_text(encoding="utf-8")
    assert result.face_count == 6
    assert result.removed_face_count == 1
    assert text.count("f 1 2 3") == 5
    assert "f 1 4 5" not in text


def test_high_detail_preset_raises_alignment_and_reconstruction_budget():
    parser = build_arg_parser()
    args = parser.parse_args(["input.mp4", "--reconstruction-preset", "high-detail"])

    applied = apply_reconstruction_preset(args, parser)

    assert applied["model_quality"] == "high"
    assert args.normal_downscale == 1
    assert args.max_frames == 720
    assert args.max_features_per_image == 160000
    assert args.simplify_triangles == 4000000
    assert args.texture_resolution == 8192


def test_detail_preset_keeps_explicit_cli_overrides():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "input.mp4",
            "--reconstruction-preset",
            "max-detail",
            "--simplify-triangles",
            "2500000",
            "--max-frames",
            "300",
        ]
    )

    applied = apply_reconstruction_preset(args, parser)

    assert "simplify_triangles" not in applied
    assert "max_frames" not in applied
    assert args.simplify_triangles == 2500000
    assert args.max_frames == 300
    assert args.model_quality == "high"
    assert args.normal_downscale == 1


def test_raw_geometry_mode_leaves_frame_unenhanced():
    frame = np.arange(4 * 5 * 3, dtype=np.uint8).reshape((4, 5, 3))

    raw = make_geometry_frame(frame, "raw", wb_gain=2.0, clahe_clip=2.0, sharpen=0.5)
    enhanced = make_geometry_frame(frame, "enhanced", wb_gain=2.0, clahe_clip=2.0, sharpen=0.5)

    assert np.array_equal(raw, frame)
    assert raw is not frame
    assert not np.array_equal(enhanced, frame)


def test_raw_base_geometry_mode_builds_raw_variant():
    parser = build_arg_parser()
    args = parser.parse_args(["input.mp4", "--base-geometry-mode", "raw"])

    variants = build_variant_specs(args)

    assert len(variants) == 1
    assert variants[0].name == "raw_brown4"
    assert variants[0].geometry_mode == "raw"
