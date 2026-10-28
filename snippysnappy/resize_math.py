# snippysnappy v1.0.3
"""Resize math shared by image and film objects (snips, text boxes, shapes)."""


def anchored_resize(handle, w0, h0, cx, cy, cos_t, sin_t, local_dx, local_dy, affects_w, affects_h, min_size=4):
    """
    Non-proportional resize of a (possibly rotated) box by one handle: the OPPOSITE edge (or corner)
    stays where it is and the dragged edge follows the pointer.

    (cx, cy)            original center, world coordinates
    cos_t, sin_t        of the box's rotation
    local_dx, local_dy  pointer relative to the original center, in the box's own (unrotated) frame
    Returns (new_w, new_h, new_cx, new_cy).
    """
    sx = -1 if "w" in handle else (1 if "e" in handle else 0)
    sy = -1 if "n" in handle else (1 if "s" in handle else 0)

    def axis(size0, sign, pointer, affects):
        # returns (new_size, new_center_offset_in_local_frame)
        if not affects or sign == 0:
            return size0, 0.0
        anchor = -sign * size0 / 2.0
        dist = pointer - anchor
        size = max(float(min_size), abs(dist))
        direction = sign if abs(dist) < min_size else (1 if dist >= 0 else -1)
        return size, anchor + direction * size / 2.0

    new_w, off_x = axis(w0, sx, local_dx, affects_w)
    new_h, off_y = axis(h0, sy, local_dy, affects_h)
    # local offset -> world (rotate by +theta)
    return new_w, new_h, cx + off_x * cos_t - off_y * sin_t, cy + off_x * sin_t + off_y * cos_t
