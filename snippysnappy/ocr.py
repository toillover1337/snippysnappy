# snippysnappy v1.0.3
"""OCR (tesseract) invocation, image preprocessing to improve OCR accuracy, and a small hex-color utility."""
from .logging_setup import log, run_logged

def ocr_image(path):
    result = run_logged(["tesseract", str(path), "stdout"], capture_output=True, text=True)
    text = result.stdout.strip()
    log.debug(f"OCR produced {len(text)} chars")
    return text



OCR_UPSCALE_FACTOR = 2       # How much to enlarge crops before OCR.
OCR_UPSCALE_SKIP_HEIGHT = 1000   # Skip upscaling only for already-large crops.


def preprocess_for_ocr(img):
    """
    Upscale (unless already large) + grayscale + autocontrast + Otsu
    binarize + auto-invert so color and background variation (multi-colored
    terminal text, translucent dark panels) can't confuse tesseract, and
    small real-world terminal font sizes get enough resolution to recognize
    reliably.

    Upscaling is based on total crop height only to skip the rare
    pathologically-large case (a whole-screen OCR).
    """
    from PIL import Image, ImageOps
    if img.height < OCR_UPSCALE_SKIP_HEIGHT:
        img = img.resize((img.width * OCR_UPSCALE_FACTOR, img.height * OCR_UPSCALE_FACTOR), Image.LANCZOS)
    gray = ImageOps.autocontrast(img.convert("L"))
    hist = gray.histogram()
    total = sum(hist)
    if total == 0:
        return img
    sum_total = sum(i * hist[i] for i in range(256))
    sum_bg = 0
    weight_bg = 0
    max_variance = -1
    threshold = 127
    for i in range(256):
        weight_bg += hist[i]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += i * hist[i]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        variance_between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if variance_between > max_variance:
            max_variance = variance_between
            threshold = i
    binary = gray.point(lambda p: 255 if p > threshold else 0)
    if binary.histogram()[0] > binary.histogram()[255]:
        binary = ImageOps.invert(binary)
    return binary.convert("RGB")


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


