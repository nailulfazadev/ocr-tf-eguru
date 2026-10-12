import sys


def selftest() -> None:
    """Dipakai untuk menguji hasil kemasan: semua modul & data harus ikut terbawa."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import cv2, numpy, PIL, pytesseract, rapidfuzz            # noqa: F401
    from google import genai                                   # noqa: F401
    from google.genai import types                             # noqa: F401
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from PySide6.QtWidgets import QApplication
    from app.gemini import GeminiReader
    from app.ui import MainWindow
    app = QApplication([])
    MainWindow()
    GeminiReader("x", "m")
    creds = Credentials(token="x")
    build("sheets", "v4", credentials=creds, cache_discovery=False)   # butuh dokumen discovery yang dibundel
    build("drive", "v3", credentials=creds, cache_discovery=False)
    print("SELFTEST OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        from app.ui import run
        run()
