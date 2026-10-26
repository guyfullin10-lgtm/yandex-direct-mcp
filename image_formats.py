"""Таблица форматов изображений Директа + предполётная проверка.

Направление 15.09.2026: adimages.add принимает практически любую картинку, но
присваивает ей Type. Привязать к объявлению можно только FIXED_IMAGE (графическое
объявление), REGULAR и WIDE (текстово-графическое). Всё остальное оседает в
библиотеке как SMALL/UNFIT: загрузка "прошла", а объявления из неё не собрать.
Ловим это до отправки, чтобы не засорять библиотеку и не гадать над причиной.

Источник: https://yandex.ru/dev/direct/doc/ru/objects/adimage
"""

import base64
import struct

# Базовые форматы графических объявлений. Директ принимает каждый из них и его
# увеличение в 2/3/4 раза — полный список в документации ровно этим и является
# (11 базовых x 4 = 44 размера).
BASE_FIXED = [
    (240, 400), (300, 250), (300, 500), (300, 600),
    (320, 50), (320, 100), (320, 480), (336, 280),
    (480, 320), (728, 90), (970, 250),
]

FIXED_SIZES = {(w * k, h * k) for w, h in BASE_FIXED for k in (1, 2, 3, 4)}

# REGULAR: стороны 450-5000 px, соотношение от 3:4 до 4:3.
# WIDE: 16:9, от 1080x607 до 5000x2812.
REGULAR_SIDE = (450, 5000)
REGULAR_RATIO = (0.74, 1.36)
WIDE_MIN = (1080, 607)
WIDE_MAX = (5000, 2812)
WIDE_RATIO_TOL = 0.03

HINT = (
    "Графическое объявление (IMAGE_AD) принимает только фиксированные размеры: "
    "240x400, 300x250, 300x500, 300x600, 320x50, 320x100, 320x480, 336x280, "
    "480x320, 728x90, 970x250 и те же форматы, увеличенные в 2/3/4 раза. "
    "Нестандартные размеры (160x600, 240x600 и подобные) Директ не принимает ни "
    "через API, ни в интерфейсе — только через Конструктор креативов, а он в API v5 "
    "доступен лишь на чтение. Передайте force=true, чтобы отправить как есть."
)


def probe_size(b64_data):
    """(width, height, format) из base64 PNG/JPEG/GIF, либо None если не распознано."""
    try:
        raw = base64.b64decode(b64_data, validate=False)
    except Exception:
        return None
    if len(raw) < 24:
        return None

    if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
        w, h = struct.unpack(">II", raw[16:24])
        return int(w), int(h), "PNG"

    if raw[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", raw[6:10])
        return int(w), int(h), "GIF"

    if raw[:2] == b"\xff\xd8":
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        i, n = 2, len(raw)
        while i + 9 < n:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xDA:  # начало данных, заголовков дальше нет
                break
            seg_len = struct.unpack(">H", raw[i + 2:i + 4])[0]
            if marker in sof:
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return int(w), int(h), "JPEG"
            if seg_len < 2:
                break
            i += 2 + seg_len
        return None

    return None


def classify(w, h):
    """Какой Type присвоит Директ: FIXED_IMAGE / WIDE / REGULAR / UNFIT."""
    if (w, h) in FIXED_SIZES:
        return "FIXED_IMAGE"

    if h and (WIDE_MIN[0] <= w <= WIDE_MAX[0] and WIDE_MIN[1] <= h <= WIDE_MAX[1]):
        if abs(w / h - 16 / 9) <= WIDE_RATIO_TOL:
            return "WIDE"

    lo, hi = REGULAR_SIDE
    if lo <= w <= hi and lo <= h <= hi and h:
        if REGULAR_RATIO[0] <= w / h <= REGULAR_RATIO[1]:
            return "REGULAR"

    return "UNFIT"


def nearest_fixed(w, h, limit=3):
    """Ближайшие допустимые форматы: сначала по соотношению сторон, потом по площади."""
    if not w or not h:
        return []
    target_ratio = w / h
    area = w * h

    def key(size):
        fw, fh = size
        return (abs(fw / fh - target_ratio), abs(fw * fh - area))

    return sorted(FIXED_SIZES, key=key)[:limit]


def describe(name, w, h, fmt, kind):
    """Человекочитаемое объяснение отказа."""
    suggestions = ["{}x{}".format(a, b) for a, b in nearest_fixed(w, h)]
    return {
        "Name": name,
        "Size": "{}x{}".format(w, h),
        "Format": fmt,
        "WouldBeType": kind,
        "Reason": (
            "{}x{} не соответствует ни одному формату Директа — картинка загрузится, "
            "но получит Type={} и её нельзя будет привязать к объявлению."
        ).format(w, h, kind),
        "NearestAllowed": suggestions,
    }


def split_by_fitness(images, force=False):
    """Делит список {name, image_data} на пригодные и отклонённые.

    Неопознанный формат пропускаем — решать должен Директ, а не наша догадка.
    """
    if force:
        return list(images), []

    accepted, rejected = [], []
    for img in images:
        probe = probe_size(img.get("image_data", ""))
        if probe is None:
            accepted.append(img)
            continue
        w, h, fmt = probe
        kind = classify(w, h)
        if kind == "UNFIT":
            rejected.append(describe(img.get("name", ""), w, h, fmt, kind))
        else:
            img = dict(img)
            img["_type"] = kind
            accepted.append(img)
    return accepted, rejected
