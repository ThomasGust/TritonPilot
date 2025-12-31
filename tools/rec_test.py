from video.cam import RemoteCameraManager
import cv2
import time
mgr = RemoteCameraManager("data\\streams.json")

print("streams:", mgr.list_available())
front = mgr.open("main_camera")
#time.sleep(3)
#Display frames like cv2.VideoCapture

while True:
    ok, frame = front.read()
    if not ok:
        print("Failed to read frame")
        time.sleep(0.1)
        continue
    cv2.imshow("Remote Camera", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break
front.release()