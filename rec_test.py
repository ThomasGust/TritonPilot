from video.cam import RemoteCameraManager
import cv2
import time
mgr = RemoteCameraManager("data\\streams.json")

print("streams:", mgr.list_available())
front = mgr.open("main_camera")
time.sleep(3)
#Display frames like cv2.VideoCapture
while True:
    ok, frame = front.read()
    if not ok:
        break
    # process frame (e.g., show with cv2.imshow)
    cv2.imshow("Front Camera", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break
front.release()