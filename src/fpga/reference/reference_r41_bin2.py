"""R41 2x horizontal binning mode of the PFO3 detector (header policy_revision == 2).

Contract (independent of RTL execution):
* every input row pair (x = 2j, 2j+1) becomes one pixel (a + b + 1) >> 1 -> a 376 x 480 image;
* the unchanged R39 detector, graph and row summaries run on that image (all thresholds, widths,
  graph tolerances and stability values in binned units; row peak / saturation counts are of
  binned pixels);
* only the wire diagnostic records (kind 0) are mapped back to 752-pixel coordinates:
  first -> 2*first, last -> 2*last + 1, centre u_q12 -> (2*u_q12 + 2048) mod 2^23
  (the 22-bit binned field doubled with half a full pixel added); v, ids and everything else unchanged.
  The geometry input uses the same converted u.
"""
import numpy as np
import reference_r39 as r39

BIN_POLICY_REVISION = 2


def bin2(image):
    a = np.asarray(image, dtype=np.int32)
    return ((a[:, 0::2] + a[:, 1::2] + 1) >> 1).astype(np.uint8)


def to_full(records):
    """Kind-0 records in 752 coordinates (copies; binned records stay valid for graph/rows)."""
    out = []
    for r in records:
        w = list(r)
        w[2], w[3] = 2 * r[2], 2 * r[3] + 1
        w[4] = (2 * (r[4] & 0x3fffff) + 2048) & 0x7fffff
        out.append(w)
    return out


def detect(image):
    """(full-coordinate kind-0 records, kind-1 patches, kind-2 rows) for a 752 x 480 frame."""
    b = bin2(image)
    records, rows = r39.candidates(b)
    patches = r39.graph_patches(records, rows)
    final = r39.final_rows(b, records, rows, patches)
    return to_full(records), patches, final
