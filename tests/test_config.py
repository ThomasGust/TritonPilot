import os
import importlib


class _FakeSocket:
    def __init__(self, peer_host: str = "127.0.0.1"):
        self.peer_host = peer_host

    def getpeername(self):
        return (self.peer_host, 6001)

    def close(self):
        pass


def _reload_config(monkeypatch):
    import config

    return importlib.reload(config)


def test_config_defaults_to_tether_when_auto_detect_disabled(monkeypatch):
    # ensure default ROV_HOST is set (doesn't need to exist on network for unit tests)
    monkeypatch.delenv("ROV_HOST", raising=False)
    monkeypatch.delenv("TRITON_ROV_HOSTS", raising=False)
    monkeypatch.setenv("TRITON_ROV_AUTO_DETECT", "0")
    cfg = _reload_config(monkeypatch)
    assert cfg.ROV_HOST.endswith(".4")
    assert cfg.PILOT_PUB_ENDPOINT.startswith("tcp://")
    assert ":6000" in cfg.PILOT_PUB_ENDPOINT


def test_config_respects_rov_host_override(monkeypatch):
    monkeypatch.setenv("ROV_HOST", "tritonpi.local")
    cfg = _reload_config(monkeypatch)
    assert cfg.ROV_HOST == "tritonpi.local"
    assert cfg.SENSOR_SUB_ENDPOINT == "tcp://tritonpi.local:6001"


def test_config_auto_detects_reachable_fallback(monkeypatch):
    monkeypatch.delenv("ROV_HOST", raising=False)
    monkeypatch.setenv("TRITON_ROV_AUTO_DETECT", "1")
    monkeypatch.setenv("TRITON_ROV_HOSTS", "192.168.1.4,tritonpi.local")

    def fake_create_connection(addr, timeout=0):
        host, _port = addr
        if host == "tritonpi.local":
            return _FakeSocket("10.0.7.192")
        raise OSError("unreachable")

    monkeypatch.setattr("socket.create_connection", fake_create_connection)
    cfg = _reload_config(monkeypatch)
    assert cfg.ROV_HOST == "10.0.7.192"


def test_config_does_not_silently_use_wifi_fallback_by_default(monkeypatch):
    monkeypatch.delenv("ROV_HOST", raising=False)
    monkeypatch.delenv("TRITON_ROV_HOSTS", raising=False)
    monkeypatch.delenv("TRITON_ROV_ALLOW_WIFI_FALLBACK", raising=False)
    monkeypatch.setenv("TRITON_ROV_AUTO_DETECT", "1")

    def fake_create_connection(addr, timeout=0):
        host, _port = addr
        if host == "tritonpi.local":
            return _FakeSocket("10.0.7.192")
        raise OSError("tether down")

    monkeypatch.setattr("socket.create_connection", fake_create_connection)
    cfg = _reload_config(monkeypatch)
    assert cfg.ROV_HOST == "192.168.1.4"


def test_transect_defaults_enable_yaw_er_and_50_percent_blue_target(monkeypatch):
    monkeypatch.delenv("TRITON_TRANSECT_ROTATION_SERVO_DEFAULT", raising=False)
    monkeypatch.delenv("TRITON_TRANSECT_TARGET_BLUE_WIDTH_PERCENT", raising=False)
    monkeypatch.delenv("TRITON_TRANSECT_TARGET_FOOTPRINT_CM", raising=False)

    cfg = _reload_config(monkeypatch)

    assert cfg.TRANSECT_ROTATION_SERVO_DEFAULT is True
    assert cfg.TRANSECT_TARGET_BLUE_WIDTH_PERCENT_DEFAULT == 50.0


def test_pilot_max_gain_defaults_to_40_percent(monkeypatch):
    monkeypatch.delenv("TRITON_PILOT_MAX_GAIN_DEFAULT", raising=False)

    cfg = _reload_config(monkeypatch)

    assert cfg.PILOT_MAX_GAIN_DEFAULT == 0.4
