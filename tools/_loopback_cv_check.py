"""Loopback proof that the topside raw-receive -> decode -> overlay path works.

Sends a test-pattern H.264 RTP stream to a local UDP port, pulls it back through
the same ReceiverProcess(mode='raw') the transect CV uses, runs the policy +
overlay, and saves one annotated frame. No ROV required.
"""
import os, subprocess, sys, time
import numpy as np, cv2

from video.gst_runtime import bootstrap_gstreamer_env
from video.gst_receiver import ReceiverProcess, RxConfig
from tracking.transect_policy import TransectModel, TransectObservation, TransectPolicy
from tracking.transect_overlay import draw_transect_overlay

PORT, W, H = 5444, 1280, 720
rt = bootstrap_gstreamer_env()
gst = str(rt.gst_launch)

def sender_cmd(enc):
    return [gst, "-q", "videotestsrc", "is-live=true", "pattern=smpte",
            "!", f"video/x-raw,width={W},height={H},framerate=30/1",
            "!", "videoconvert", "!", enc,
            "!", "rtph264pay", "config-interval=1", "pt=96",
            "!", "udpsink", "host=127.0.0.1", f"port={PORT}", "sync=false"]

send = None
for enc in ("x264enc tune=zerolatency bitrate=2000", "openh264enc"):
    cmd = sender_cmd(enc.split()[0])
    # inline encoder props for x264enc
    if enc.startswith("x264enc"):
        cmd = [gst, "-q", "videotestsrc", "is-live=true", "pattern=smpte",
               "!", f"video/x-raw,format=I420,width={W},height={H},framerate=30/1,colorimetry=1:4:0:0",
               "!", "x264enc", "tune=zerolatency", "bitrate=2000",
               "!", "rtph264pay", "config-interval=1", "pt=96",
               "!", "udpsink", "host=127.0.0.1", f"port={PORT}", "sync=false"]
    try:
        send = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.7)
        if send.poll() is None:
            print(f"sender up using {enc.split()[0]}")
            break
        send = None
    except Exception as e:
        print("sender err", e); send = None
if send is None:
    print("FAIL: could not start a test sender (no x264enc/openh264enc?)"); sys.exit(2)

rx = ReceiverProcess(RxConfig(name="loop", codec="h264", port=PORT, mode="raw",
                              width=W, height=H, bind_address="127.0.0.1",
                              extra={"receiver_kill_port_users": False}))
rx.start()
model = TransectModel(); pol = TransectPolicy(model)
obs = TransectObservation(blue_found=True, blue_cx=0.5, blue_cy=0.5,
                          blue_fraction=model.nominal_blue_fraction, fit_quality=0.95)
ok = False
deadline = time.time() + 8.0
while time.time() < deadline:
    pkt = rx.latest_frame_packet()
    if pkt is not None and len(pkt.data) == W * H * 3:
        frame = np.frombuffer(pkt.data, np.uint8).reshape((H, W, 3)).copy()
        for _ in range(model.min_lock_frames):
            est = pol.evaluate(obs)
        draw_transect_overlay(frame, model, est, obs)
        out = os.path.join(os.environ.get("TEMP", "."), "loopback_cv.jpg")
        cv2.imwrite(out, frame)
        print(f"OK: decoded a {W}x{H} frame, overlay drawn, saved {out} (lock={est.lock_state})")
        ok = True
        break
    time.sleep(0.05)
rx.stop()
try: send.terminate()
except Exception: pass
sys.exit(0 if ok else 3)
