/* Display-only exact LINE_8 footprints. No retained memory or measurement. */
#include <stdint.h>
#include <stddef.h>
#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

EXPORT uint32_t sl_marker_r15c_abi(void) { return 1u; }

static int valid_group(const int64_t *p, size_t n, int accepted)
{
    size_t i;
    if (n > 2400u || (n && !p)) return 0;
    for (i=0; i<n; ++i) {
        if (p[3u*i]<0 || p[3u*i]>=752*4096 || p[3u*i+1u]<0 ||
                p[3u*i+1u]>=480 || p[3u*i+2u]<0 || p[3u*i+2u]>1 ||
                (accepted && p[3u*i+2u])) return 0;
    }
    return 1;
}

static void paint_group(uint8_t *canvas, const int64_t *p, size_t n,
                        int32_t cx, int32_t cy, const uint8_t *color)
{
    static const int8_t circle[5][2]={{0,-1},{-1,0},{0,0},{1,0},{0,1}};
    static const int8_t cross[9][2]={{-2,-2},{-1,-1},{0,0},{1,1},{2,2},
                                   {-2,2},{-1,1},{1,-1},{2,-2}};
    size_t i,j;
    for (i=0; i<n; ++i) {
        int32_t x=(int32_t)(p[3u*i]/4096)+cx;
        int32_t y=(int32_t)p[3u*i+1u]+cy;
        size_t count=p[3u*i+2u] ? 9u : 5u;
        const int8_t (*offsets)[2]=p[3u*i+2u] ? cross : circle;
        for (j=0; j<count; ++j) {
            int32_t xx=x+offsets[j][0], yy=y+offsets[j][1];
            if (xx>=0 && xx<1280 && yy>=0 && yy<720) {
                size_t off=3u*((size_t)yy*1280u+(size_t)xx);
                canvas[off]=color[0];canvas[off+1u]=color[1];canvas[off+2u]=color[2];
            }
        }
    }
}

EXPORT int sl_marker_r15c(uint8_t *canvas, size_t bytes,
        const int64_t *measurement, size_t nm, const int64_t *preview, size_t np,
        const int64_t *rejected, size_t nr, int32_t cx, int32_t cy,
        const uint8_t *colors)
{
    if (!canvas || !colors || bytes!=1280u*720u*3u || nm>2400u || np>2400u ||
            nr>2400u || nm+np+nr>2400u || cx<0 || cx>1280-752 || cy<0 ||
            cy>720-480) return 1;
    /* Reject before painting, including invalid later groups. */
    if (!valid_group(measurement,nm,1) || !valid_group(preview,np,1) ||
            !valid_group(rejected,nr,0)) return 2;
    paint_group(canvas,measurement,nm,cx,cy,colors);
    paint_group(canvas,preview,np,cx,cy,colors+3);
    paint_group(canvas,rejected,nr,cx,cy,colors+6);
    return 0;
}
