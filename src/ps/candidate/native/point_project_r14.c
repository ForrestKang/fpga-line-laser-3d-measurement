/* Display-only fused projection. The established raster keeps colour, dy-pass
 * order, range checking and round-to-nearest semantics. No retained pointers. */
#include "../../structured_light_system_algorithm_v1/src/python/structured_light/scan3d/native/point_raster.c"
#include <float.h>

EXPORT uint32_t sl_point_project_r14_abi(void) { return 1u; }

EXPORT int sl_point_project_r14(
    const double *xyz, size_t count, const double *right, const double *up,
    int32_t width, int32_t height, uint8_t *panel, size_t panel_stride,
    double *px, double *py, int32_t *sx, int32_t *sy, uint8_t *colors,
    size_t capacity)
{
    double low[3], high[3], center[3], minx=0., maxx=0., miny=0., maxy=0.;
    double spanx, spany, scalex, scaley, scale, bound=0.;
    size_t i;
    int j;
    if (!xyz || !right || !up || !panel || !px || !py || !sx || !sy || !colors ||
            count==0 || count>capacity || width<7 || height<7) return 3;
    if (fegetround()!=FE_TONEAREST) return 1;
    for (j=0;j<3;++j) {
        if (!isfinite(right[j]) || !isfinite(up[j])) return 2;
        low[j]=high[j]=xyz[j];
    }
    for (i=0;i<count;++i) {
        for (j=0;j<3;++j) {
            double v=xyz[3u*i+(size_t)j];
            if (!isfinite(v)) return 2;
            if (v<low[j]) low[j]=v;
            if (v>high[j]) high[j]=v;
        }
    }
    for (j=0;j<3;++j) {
        center[j]=(low[j]+high[j])*0.5;
        if (!isfinite(center[j])) return 2;
    }
    for (i=0;i<count;++i) {
        double a=xyz[3u*i]-center[0], b=xyz[3u*i+1u]-center[1];
        double c=xyz[3u*i+2u]-center[2];
        double x=(a*right[0]+b*right[1])+c*right[2];
        double y=(a*up[0]+b*up[1])+c*up[2];
        double magnitude=fabs(a*right[0])+fabs(b*right[1])+fabs(c*right[2])+
            fabs(a*up[0])+fabs(b*up[1])+fabs(c*up[2]);
        if (!isfinite(x) || !isfinite(y) || !isfinite(magnitude)) return 2;
        if (magnitude>bound) bound=magnitude;
        px[i]=x;py[i]=y;
        if (!i) {minx=maxx=x;miny=maxy=y;}
        else {
            if (x<minx) minx=x;
            if (x>maxx) maxx=x;
            if (y<miny) miny=y;
            if (y>maxy) maxy=y;
        }
    }
    spanx=fmax(maxx-minx,1.);spany=fmax(maxy-miny,1.);
    scalex=((double)width-112.)/spanx;scaley=((double)height-106.)/spany;
    scale=fmin(scalex,scaley);
    if (!isfinite(scale)) return 2;
    /* NumPy dot may use a differently associated three-term sum. Do not paint
     * a rounding-boundary case: retain the old NumPy projection/raster path.
     * This conservative guard scales with cancellation in the dot products. */
    bound=128.*DBL_EPSILON*(1.+bound*fabs(scale)+width+height);
    if (bound>0.01) return 4;
    for (i=0;i<count;++i) {
        double x=(double)width*0.52+px[i]*scale;
        double y=(double)height*0.48-py[i]*scale;
        if (!isfinite(x) || !isfinite(y)) return 2;
        if (fabs((x-floor(x))-0.5)<=bound || fabs((y-floor(y))-0.5)<=bound)
            return 4;
    }
    return sl_point_raster_v1(xyz,px,py,count,scale,low[2],
        fmax(high[2]-low[2],1e-9),width,height,panel,panel_stride,sx,sy,colors,capacity);
}
