from video.rov_streams import ROVStreams
from video.gst_receiver import RxConfig, ReceiverManager

class ROVCameras:

    def __init__(self):
        self.rcp_interface = ROVStreams()
        