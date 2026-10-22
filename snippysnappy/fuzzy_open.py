# snippysnappy v1.0.0
"""Fuzzy file-open helpers: --nil arg parsing, junk-file scoring, and the fuzzy search used by --open prompt / the Tabs panel file picker."""
from .constants import NIL_CANVAS_DEFAULT_WIDTH, NIL_CANVAS_DEFAULT_HEIGHT, NIL_CANVAS_DEFAULT_COLOR
import os
import re
from pathlib import Path

from .constants import FUZZ_DIR, IMAGE_FILE_EXTENSIONS, OPEN_PROMPT_MAX_SCAN_FILES, JUNK_PENALTY_WEIGHT
from .logging_setup import log

_NIL_DIMENSIONS_RE = re.compile(r"^(\d+)[xX](\d+)$")
_NIL_COLOR_CODES = {"w": "white", "white": "white", "b": "black", "black": "black", "t": "transparent", "transparent": "transparent"}


def parse_nil_args(tokens):
    """
    Parses --nil/-n's optional sub-arguments (any order): a WxH dimension
    token (e.g. "1200x1000") and/or a color code (w/white, b/black,
    t/transparent). Falls back to NIL_CANVAS_DEFAULT_WIDTH/HEIGHT/COLOR for
    whichever isn't given. Unrecognized tokens are warned about and ignored
    rather than treated as a hard error.
    """
    width, height = NIL_CANVAS_DEFAULT_WIDTH, NIL_CANVAS_DEFAULT_HEIGHT
    color = NIL_CANVAS_DEFAULT_COLOR
    for token in tokens:
        m = _NIL_DIMENSIONS_RE.match(token.strip())
        if m:
            width, height = int(m.group(1)), int(m.group(2))
            continue
        code = _NIL_COLOR_CODES.get(token.strip().lower())
        if code:
            color = code
            continue
        log.warning(f"--nil: unrecognized argument {token!r} (expected WxH or w/b/t) - ignoring")
    return width, height, color


def make_nil_placeholder(path, color):
    """
    --nil takes no screenshot, so there's no real "capture" underneath the
    canvas layer at all - this is a throwaway 1x1 file purely so
    SelectorApp has something to open at construction time. main()'s --nil
    branch sets app._no_real_capture = True right after, which tells
    _build_canvas_composite to never actually paste this placeholder in -
    the canvas background alone provides the blank canvas. The color
    argument only matters for consistency/debugging, not for what's shown.
    """
    from PIL import Image
    fill = {"white": (255, 255, 255), "black": (0, 0, 0)}.get(color, (255, 255, 255))
    Image.new("RGB", (1, 1), fill).save(path)


def is_image_file(path):
    return path.suffix.lower() in IMAGE_FILE_EXTENSIONS


JUNK_PATH_MARKERS = [
    "/.cache/", "/.thumbnails/", "/.icons/", "/icons/", "/.local/share/icons/",
    "/.git/", "/node_modules/", "/.npm/", "/__pycache__/", "/.cargo/", "/.rustup/",
    "/site-packages/", "/.mozilla/", "/.thunderbird/", "/.var/app/", "/snap/",
    "/.steam/", "/.wine/", "/.local/share/trash/", "/usr/share/icons/",
    "/usr/share/pixmaps/", "/usr/lib/", "/usr/share/doc/",
]
# Cheap (path/filename-only, no file I/O beyond what's already done for
# mtime) signals that a file is more likely an incidental system/app
# artifact - icons, cache thumbnails, bundled theme assets, etc. - than
# something the user actually took or wants to open. Each list is checked
# independently and only the strongest single hit in each counts (so one
# obviously-junky path doesn't get double-penalized further for also
# having a junky filename on top of it), keeping the penalty proportionate.
JUNK_NAME_MARKERS = ["icon", "thumbnail", "thumb", "favicon"]


def junk_score(path):
    """
    0 = no junk signal at all; higher = more likely an incidental app/
    system artifact rather than a real photo/screenshot. Used only to
    DEPRIORITIZE - see JUNK_PENALTY_WEIGHT - never to exclude a file
    outright, so it's still findable by an explicit search.
    """
    s = str(path).lower()
    score = 0
    if any(marker in s for marker in JUNK_PATH_MARKERS):
        score += 3
    name = path.name.lower()
    if any(marker in name for marker in JUNK_NAME_MARKERS):
        score += 2
    if name.startswith("."):
        score += 1
    return score


def get_default_open_dir():
    return Path(FUZZ_DIR).expanduser() if FUZZ_DIR else (Path.home() / "Pictures")


def resolve_open_path(path_str, parent_dir=None):
    """
    Resolves a --open PATH argument (or `--open prompt`'s typed path):
    absolute paths are used as-is; anything else is resolved relative to
    `parent_dir` (defaults to get_default_open_dir(), i.e. FUZZ_DIR or
    ~/Pictures) - NOT the shell's current working directory.
    """
    p = Path(path_str).expanduser()
    if p.is_absolute():
        return p
    base = parent_dir if parent_dir is not None else get_default_open_dir()
    return Path(base) / p


def fuzzy_match(query, candidate):
    """
    Simple ordered-subsequence fuzzy match (fzf-style, simplified): every
    character of `query` (case-insensitive) must appear, in order,
    somewhere in `candidate`, though not necessarily contiguously. Returns
    (score, matched_indices) - higher score is a better match - or None if
    the query doesn't match at all (this is the "forgiving but not so
    forgiving it matches junk" boundary: a query that skips around too much
    still matches, but scores low enough to sort behind tight matches).
    Consecutive matched characters and matches right after a path
    separator/word boundary score higher, so tight substring-like matches
    consistently outrank scattered ones.
    """
    if not query:
        return (0, [])
    q = query.lower()
    c = candidate.lower()
    qi = 0
    matched = []
    score = 0.0
    last_match_idx = -2
    for ci, ch in enumerate(c):
        if qi < len(q) and ch == q[qi]:
            matched.append(ci)
            if ci == last_match_idx + 1:
                score += 3
            elif ci == 0 or c[ci - 1] in "/_-. ":
                score += 2
            else:
                score += 1
            last_match_idx = ci
            qi += 1
    if qi < len(q):
        return None
    score -= (len(c) - len(q)) * 0.01
    return (score, matched)


def fuzzy_search_images(root_dir, query, limit=30):
    """
    Searches every image file under root_dir (recursively, capped at
    OPEN_PROMPT_MAX_SCAN_FILES so a huge root like "/" can't hang the
    picker indefinitely) and fuzzy-matches `query` against each file,
    returning the top `limit` matches as (Path, matched_indices) sorted
    best-first (indices are positions within the full path string, for
    highlighting). With an empty query, returns the most recently modified
    images instead (a reasonable default list to browse).

    The FILENAME is matched first and given a large score bonus over a
    full-path match - matching the full path directly (the original
    approach) let a query match by stitching together characters from
    unrelated directory components (e.g. "logo" matching an 'l' from
    ".../uploads/..." plus a 'g' and 'o' from ".../background2.png" later
    in the same path), which could easily outscore a file whose actual
    NAME matches almost perfectly. Full-path matching is now only a
    fallback for files whose name alone doesn't match at all, so a
    directory-name-based search still finds something, just ranked well
    In both browse mode and search results, files with a nonzero
    junk_score (app icons, cache thumbnails, bundled theme assets, etc. -
    see JUNK_PATH_MARKERS/JUNK_NAME_MARKERS) are pushed down rather than
    excluded, so they don't dominate a fresh "browse the home dir" list or
    flood out real matches during an unrelated search, but still surface
    if you actually search for one.
    """
    root_dir = Path(root_dir)
    if not root_dir.is_dir():
        return []
    candidates = []
    scanned = 0
    try:
        for p in root_dir.rglob("*"):
            scanned += 1
            if scanned > OPEN_PROMPT_MAX_SCAN_FILES:
                log.warning(f"--open prompt: hit the {OPEN_PROMPT_MAX_SCAN_FILES}-file scan cap under {root_dir} - results may be incomplete")
                break
            if p.is_file() and is_image_file(p):
                candidates.append(p)
    except (PermissionError, OSError):
        log.warning(f"--open prompt: could not fully walk {root_dir} (permission or I/O error) - showing partial results")
    log.debug(f"--open prompt: scanned {scanned} filesystem entries under {root_dir}, found {len(candidates)} image file(s)")
    if not query.strip():
        # Junk tier first (ascending junk_score, so 0 = real files sort
        # first), newest-first within each tier - a pile of old icon/cache
        # files can never bury today's actual screenshots this way, no
        # matter how their mtimes compare.
        candidates.sort(key=lambda p: (junk_score(p), -(p.stat().st_mtime if p.exists() else 0)))
        junk_count = sum(1 for p in candidates[:limit] if junk_score(p) > 0)
        if junk_count:
            log.debug(f"--open prompt: browse list includes {junk_count} likely-junk file(s) (deprioritized, not hidden)")
        return [(p, []) for p in candidates[:limit]]
    FILENAME_MATCH_BONUS = 1000   # keeps every filename match ranked above every path-only fallback match
    scored = []
    name_matches = 0
    for p in candidates:
        full_path_str = str(p)
        name_offset = len(full_path_str) - len(p.name)   # where the filename starts within the full path string
        penalty = junk_score(p) * JUNK_PENALTY_WEIGHT
        name_match = fuzzy_match(query, p.name)
        if name_match is not None:
            score, name_idxs = name_match
            full_idxs = [i + name_offset for i in name_idxs]
            scored.append((score + FILENAME_MATCH_BONUS - penalty, p, full_idxs))
            name_matches += 1
            continue
        path_match = fuzzy_match(query, full_path_str)
        if path_match is not None:
            score, idxs = path_match
            scored.append((score - penalty, p, idxs))
    scored.sort(key=lambda t: t[0], reverse=True)
    log.debug(
        f"--open prompt: {len(scored)} of {len(candidates)} image file(s) matched query {query!r} "
        f"({name_matches} by filename, {len(scored) - name_matches} by path fallback only)"
    )
    return [(p, idxs) for score, p, idxs in scored[:limit]]


