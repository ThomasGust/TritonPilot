import sys
from PyQt6.QtWidgets import QApplication
from gui.main_window import MainWindow
from config import STREAMS_FILE

def main():
    app = QApplication(sys.argv)

    win = MainWindow(streams_path=str(STREAMS_FILE))
    win.show()

    # PyQt6 uses exec(), not exec_()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

