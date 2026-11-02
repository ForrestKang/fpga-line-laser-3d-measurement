/* Storage-only comparisons over strictly decoded immutable observations.
 * memcpy loads intentionally support unaligned MBR1 arrays on ARMv7.
 * No pixel processing, compression, persistence or durability authority. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <math.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif
API uint32_t storage15_abi(void) { return 0x150001; }
static uint32_t u32(const void *p, size_t i) {
    uint32_t v; memcpy(&v, (const uint8_t *)p+4*i, 4); return v;
}
static int64_t i64(const void *p, size_t i) {
    int64_t v; memcpy(&v, (const uint8_t *)p+8*i, 8); return v;
}
static double f64(const void *p, size_t i) {
    double v; memcpy(&v, (const uint8_t *)p+8*i, 8); return v;
}
/* arrays: row,center,accepted,branch,width,contrast,edge,peak,start,end,id,reason.
 * Return the same first field error as the Python observation comparator. */
API int storage15_observations(const void *records, size_t total, size_t count,
                              int preview, const void **a, const int64_t *reasons) {
    size_t k, out=0; uint32_t bad=0;
    if (total>180480 || count>7680 || (preview!=0 && preview!=1)) return 100;
    for (k=0;k<total;k++) {
        const uint8_t *r=(const uint8_t *)records+64*k;
        uint32_t d=u32(r,13), start=u32(r,2), end=u32(r,3);
        uint32_t code=(d>>(preview?16:8))&255;
        uint32_t branch=(u32(r,14)>>(preview?16:0))&65535;
        if (d&255) continue;
        if (out>=count || code>=8) return 100;
        if (i64(a[0],out)!=(int64_t)u32(r,1)) bad|=1u<<0;
        if (i64(a[1],out)!=(int64_t)u32(r,4)) bad|=1u<<1;
        if ((((const uint8_t *)a[2])[out]!=0)!=((d&(preview?0x04000000:0x02000000))!=0)) bad|=1u<<2;
        if (i64(a[3],out)!=(branch==65535?-1:(int64_t)branch)) bad|=1u<<3;
        if (i64(a[4],out)!=(int64_t)end-(int64_t)start+1) bad|=1u<<4;
        if (f64(a[5],out)!=(double)u32(r,10)/4096.0) bad|=1u<<5;
        if (f64(a[6],out)!=(double)u32(r,11)/4096.0) bad|=1u<<6;
        if (i64(a[7],out)!=(int64_t)(u32(r,15)>>24)) bad|=1u<<7;
        if (i64(a[8],out)!=(int64_t)start) bad|=1u<<8;
        if (i64(a[9],out)!=(int64_t)end) bad|=1u<<9;
        if (i64(a[10],out)!=(int64_t)u32(r,0)) bad|=1u<<10;
        if ((int64_t)(int32_t)u32(a[11],out)!=reasons[code]) bad|=1u<<11;
        out++;
    }
    if (out!=count) return 100;
    for (k=0;k<12;k++) if (bad&(1u<<k)) return (int)k+1;
    return 0;
}
/* Compare gathered XYZ without materializing masks or selected point arrays.
 * Byte comparison preserves signed zero and all original float64 bits. */
API int storage15_points(const void *source, const void *mask, size_t count,
                         const void *points, size_t kept, int axis) {
    size_t i, out=0;
    if (count>7680 || kept>count || (axis!=0 && axis!=1)) return 100;
    for (i=0;i<count;i++) {
        const uint8_t *row=(const uint8_t *)source+24*i;
        if (!((const uint8_t *)mask)[i]) continue;
        if (axis && (!isfinite(f64(row,0)) || !isfinite(f64(row,1)) || !isfinite(f64(row,2)))) continue;
        if (out>=kept || memcmp(row,(const uint8_t *)points+24*out,24)) return 1;
        out++;
    }
    return out==kept?0:1;
}
