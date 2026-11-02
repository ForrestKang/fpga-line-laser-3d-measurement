"""R33 single-3x3 raw shared-window integer oracle, not R32 multiscale."""
import numpy as np


def features(image):
    a = np.asarray(image, dtype=np.int64)
    if a.ndim != 2 or min(a.shape) < 3 or np.any((a < 0) | (a > 255)):
        raise ValueError("expected >=3x3 unsigned 8-bit raster")
    h, w = a.shape
    p = [a[j:j+h-2, i:i+w-2] for j in range(3) for i in range(3)]
    p00,p01,p02,p10,p11,p12,p20,p21,p22=p
    s=p00+2*p01+p02+2*p10+4*p11+2*p12+p20+2*p21+p22
    gx=-p00+p02-2*p10+2*p12-p20+p22
    gy=-p00-2*p01-p02+p20+2*p21+p22
    def half(v): return np.where(v < 0, -((-v+1)//2), (v+1)//2)
    fields=[s,half(gx),half(gy),
        p00-2*p01+p02+2*p10-4*p11+2*p12+p20-2*p21+p22,
        p00-p02-p20+p22,
        p00+2*p01+p02-2*p10-4*p11-2*p12+p20+2*p21+p22,
        sum((v >= 250).astype(np.int64) for v in p)]
    out=np.zeros((h,w,7),dtype=np.int64)
    out[1:-1,1:-1]=np.stack(fields,axis=-1)
    return out
