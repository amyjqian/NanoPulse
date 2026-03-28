"""
NanoPulse – Trading Platform
Entry point.
"""
import logging
import sys

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

from src.utils.logger import setup_logging
from src.ui.main_window import MainWindow

log = logging.getLogger(__name__)


def main() -> None:
    setup_logging()
    log.info("NanoPulse starting …")

    app = QApplication(sys.argv)
    app.setApplicationName("NanoPulse")
    app.setOrganizationName("NanoPulse")


    # Default application font
    app.setFont(QFont("SF Pro Display, Helvetica Neue, Arial", 11))

    window = MainWindow()
    window.show()

    log.info("Main window shown – entering event loop")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

