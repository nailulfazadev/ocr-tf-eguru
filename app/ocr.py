"""Pra-proses gambar + Tesseract. Berjalan lokal, tanpa kuota."""
from __future__ import annotations
import io
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageOps
from pytesseract import Output

_LANG = None


def _setup_tesseract() -> None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    exe = "tesseract.exe" if os.name == "nt" else "tesseract"
    bundled = base / "vendor" / "tesseract"
    if (bundled / exe).exists():                      # Tesseract ikut dibundel di aplikasi
        pytesseract.pytesseract.tesseract_cmd = str(bundled / exe)
        if (bundled / "tessdata").exists():
            os.environ["TESSDATA_PREFIX"] = str(bundled / "tessdata")
        return
    # aplikasi GUI di Mac/Windows tidak mewarisi PATH terminal -> cek lokasi umum
    for p in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract",
              r"C:\Program Files\Tesseract-OCR\tesseract.exe"):
        if Path(p).exists():
            pytesseract.pytesseract.tesseract_cmd = p
            return


def _lang() -> str:
    global _LANG
    if _LANG is None:
        _setup_tesseract()
        have = set(pytesseract.get_languages(config=""))
        _LANG = "ind+eng" if {"ind", "eng"} <= have else "eng"
    return _LANG


def check_ready() -> str:
    """Pastikan Tesseract terpasang. Melempar TesseractNotFoundError bila belum. Mengembalikan bahasa yang dipakai."""
    return _lang()


def preprocess(img_bytes: bytes) -> np.ndarray:
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes)))
    if im.mode in ("RGBA", "LA", "P"):                # latar transparan -> putih
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    gray = np.array(im.convert("L"))
    h, w = gray.shape
    if w < 1200:                                      # screenshot kecil -> perbesar
        s = 1600 / w
        gray = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    if gray.mean() < 110:                             # mode gelap -> balik warna
        gray = 255 - gray
    return gray


def _ocr_once(gray: np.ndarray, psm: int) -> tuple[str, float]:
    d = pytesseract.image_to_data(gray, lang=_lang(), config=f"--psm {psm}", output_type=Output.DICT)
    lines: dict[tuple, list[str]] = {}
    confs: list[float] = []
    for i, word in enumerate(d["text"]):
        word = word.strip()
        if not word:
            continue
        lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append(word)
        c = float(d["conf"][i])
        if c >= 0:
            confs.append(c)
    text = "\n".join(" ".join(ws) for ws in lines.values())
    return text, (sum(confs) / len(confs) if confs else 0.0)


def run_ocr(img_bytes: bytes) -> tuple[str, float]:
    gray = preprocess(img_bytes)
    best = ("", 0.0)
    for psm in (6, 4):                                # coba 2 mode tata letak, ambil yang terbaik
        text, conf = _ocr_once(gray, psm)
        if conf > best[1]:
            best = (text, conf)
        if best[1] >= 80:
            break
    return best
