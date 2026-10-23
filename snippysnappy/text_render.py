# snippysnappy v1.0.1
"""Text-tool font loading/caching and word-wrap/rendering helpers used when baking Text elements into the final image."""
import re
from pathlib import Path

from .constants import TEXT_FONT_FAMILIES, TEXT_FONT_FAMILY_DEFAULT, TEXT_FONT_SIZE_DEFAULT, _FONT_CACHE, _FONT_MISSING_WARNED
from .logging_setup import log

def load_text_font(family, size):
    """
    Loads (and caches) a PIL font for one of TEXT_FONT_FAMILIES at the given
    pixel size, falling back to PIL's built-in bitmap font (which ignores
    size) if none of that family's candidate paths exist.
    """
    from PIL import ImageFont
    if family not in TEXT_FONT_FAMILIES:
        family = TEXT_FONT_FAMILY_DEFAULT
    size = max(1, int(size))
    key = (family, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    font = None
    for path in TEXT_FONT_FAMILIES[family]["paths"]:
        if Path(path).exists():
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        if family not in _FONT_MISSING_WARNED:
            _FONT_MISSING_WARNED.add(family)
            log.warning(f"No {family} TTF found in TEXT_FONT_FAMILIES[{family!r}]['paths'] - falling back to PIL's default bitmap font (won't respect size)")
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def load_serif_font(size):
    return load_text_font("Serif", size)


def _font_ascent(font):
    try:
        return font.getmetrics()[0]
    except Exception:
        return font.getbbox("A")[3]


def _group_len(pieces, strip):
    merged = []
    for text, _c, font in pieces:
        key = (getattr(font, "path", None) or id(font), getattr(font, "size", None))
        if merged and merged[-1][0] == key:
            merged[-1][1] += text
        else:
            merged.append([key, text, font])
    if strip and merged:
        merged[-1][1] = merged[-1][1].rstrip()
    return sum(f.getlength(t) for _k, t, f in merged if t)


def _wrap_line_tokens(line, avail_w):
    """
    line: a list of (text, color, font) segments making up ONE explicit
    line (already split on newlines - see draw_text_runs). Splits it into
    however many sub-lines fit within avail_w, breaking ONLY at whitespace.
    A word is never split mid-word, even when it straddles several
    differently-colored/fonted runs (including one-character runs): pieces
    are grouped into whole words (plus their trailing whitespace) across
    run boundaries first, and each group is measured and placed as a unit.
    """
    pieces = []
    for text, color, font in line:
        for piece in re.findall(r"\S+\s*|\s+", text):
            if piece:
                pieces.append((piece, color, font))
    if not pieces:
        return [[]]
    groups = []
    cur = []
    for piece in pieces:
        if cur and cur[-1][0][-1].isspace() and not piece[0][0].isspace():
            groups.append(cur)
            cur = []
        cur.append(piece)
    groups.append(cur)
    sublines = []
    current = []
    current_w = 0.0
    for grp in groups:
        full_w = _group_len(grp, False)
        stripped_w = _group_len(grp, True)
        has_text = any(p[0].strip() for p in grp)
        if current and has_text and (current_w + stripped_w) > avail_w:
            sublines.append(current)
            current = []
            current_w = 0.0
        current.extend(grp)
        current_w += full_w
    sublines.append(current)
    return sublines


def draw_text_runs(draw, stroke, x0=4, y0=2, box_w=None):
    """
    Draws a text element's content onto `draw` - a sequence of "runs" (see
    _extract_text_runs), each with its own optional color/font override on
    top of the element's base color/font_family, laid out left to right
    with baselines aligned per line. Newlines in the text (Shift+Enter)
    ALWAYS start a new line - explicit line breaks are never merged back
    together regardless of how much room is left. Whether that explicit
    line then further wraps automatically depends on box_w: if given, and
    the line doesn't fit within it, it's greedily word-wrapped at
    whitespace only (never mid-word, even across a run boundary - see
    _wrap_line_tokens); if box_w is None, no automatic wrapping happens at
    all (the caller doesn't know/care about a box width). A line short
    enough to already fit is never wrapped, so Shift+Enter alone still
    fully controls line breaks in a box that's wide enough. Lines that
    don't fit vertically within the element's own height are naturally
    clipped by the caller's patch bounds (PIL silently drops anything
    drawn outside an image's own size), not specially handled here. A
    stroke with no "runs" key (anything created before per-run formatting
    existed, or pasted from the register without it) is drawn as one
    plain run of its "text". Line pitch is the tallest ascent on the line
    + 4px, matching what PIL's own multiline_text used before.

    box_w, if given, enables stroke["text_align"] ("left" | "center" |
    "right", default "left"): each LINE's own total rendered width is
    measured and offset within box_w accordingly - alignment is per-line
    within the box, not just an overall block shift, so lines of
    different lengths each center/right-align independently, matching how
    a word processor aligns a paragraph rather than treating the whole
    block as one rigid unit.
    """
    base_color = stroke.get("color", "#000000")
    base_family = stroke.get("font_family", TEXT_FONT_FAMILY_DEFAULT)
    size = stroke.get("font_size", TEXT_FONT_SIZE_DEFAULT)
    align = stroke.get("text_align", "left")
    runs = stroke.get("runs") or [{"text": stroke.get("text", ""), "color": None, "font": None, "size": None}]
    lines = [[]]
    for run in runs:
        parts = run["text"].split("\n")
        for i, part in enumerate(parts):
            if i > 0:
                lines.append([])
            if part:
                run_font = load_text_font(run.get("font") or base_family, run.get("size") or size)
                lines[-1].append((part, run.get("color") or base_color, run_font))
    if box_w is not None:
        avail_w = max(1, box_w - x0 * 2)
        wrapped_lines = []
        for line in lines:
            wrapped_lines.extend(_wrap_line_tokens(line, avail_w))
        lines = wrapped_lines
    y = y0
    for line in lines:
        ascents = [_font_ascent(font) for _, _, font in line]
        max_ascent = max(ascents) if ascents else _font_ascent(load_text_font(base_family, size))
        x = x0
        if box_w is not None and align != "left":
            line_w = sum(font.getlength(text) for text, _, font in line)
            free_space = max(0, box_w - x0 * 2 - line_w)
            if align == "center":
                x = x0 + free_space / 2
            elif align == "right":
                x = x0 + free_space
        for (text, color, font), ascent in zip(line, ascents):
            draw.text((x, y + max_ascent - ascent), text, fill=color, font=font)
            x += font.getlength(text)
        y += max_ascent + 4
