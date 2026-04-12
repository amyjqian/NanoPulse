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


def handle_exception(exc_type, exc_value, exc_traceback):
    """Log unhandled exceptions to the main logger."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    log.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


def main() -> None:
    # Set global exception handler
    sys.excepthook = handle_exception

    setup_logging()
    log.info("NanoPulse starting …")

    app = QApplication(sys.argv)
    app.setApplicationName("nanoPulse")
    app.setOrganizationName("nanoPulse")
    app.setStyle("Fusion")

    # Default application font
    font = QFont()
    font.setFamilies(["SF Pro Display", "Helvetica Neue", "Arial"])
    font.setPointSize(11)
    app.setFont(font)

    window = MainWindow()
    window.show()

    log.info("Main window shown – entering event loop")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

