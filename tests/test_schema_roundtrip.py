from schema.pilot_common import PilotFrame, PilotAxes, PilotButtons

def test_pilot_frame_roundtrip():
    f = PilotFrame(
        schema=1,
        seq=123,
        ts=1.234,
        axes=PilotAxes(lx=0.1, ly=-0.2, rx=0.3, ry=-0.4, lt=0.5, rt=0.6),
        buttons=PilotButtons(a=True, b=False, x=True, y=False),
        dpad=(1, -1),
        edges={"menu": "down"},
        modes={"example": 1},
    )
    d = f.to_dict()
    f2 = PilotFrame.from_dict(d)
    assert f2.seq == f.seq
    assert abs(f2.axes.lx - f.axes.lx) < 1e-6
    assert f2.buttons.a is True
    assert f2.dpad == (1, -1)
    assert f2.edges["menu"] == "down"
