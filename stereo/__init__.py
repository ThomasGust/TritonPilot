"""Stereo camera capture infrastructure for TritonPilot."""

from stereo.capture import StereoCaptureSession
from stereo.pairs import StereoPairConfig, load_stereo_pairs

__all__ = ["StereoCaptureSession", "StereoPairConfig", "load_stereo_pairs"]
