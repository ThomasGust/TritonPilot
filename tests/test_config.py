import os
import importlib

def test_config_defaults():
    # ensure default ROV_HOST is set (doesn't need to exist on network for unit tests)
    os.environ.pop("ROV_HOST", None)
    cfg = importlib.reload(__import__("config"))
    assert cfg.ROV_HOST.endswith(".3")
    assert cfg.PILOT_PUB_ENDPOINT.startswith("tcp://")
    assert ":6000" in cfg.PILOT_PUB_ENDPOINT
