/* Zero-distortion double geometry; flagged decisions use frozen Python. */
#include <stdint.h>
#include <math.h>
#include <stddef.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif

API void geometry_r21(const double *rows,const double *q,const uint8_t *accepted,
                     size_t n,const double *p,int has_limits,double *xyz,
                     int32_t *reason,uint8_t *outside,uint8_t *fallback) {
    size_t i;int j;
    const double floor=1e-6;
    for(i=0;i<n;++i) {
        double x,y,scale,sx,sy,sz,length,dx,dy,dz,cosine,distance,tolerance;
        reason[i]=0;outside[i]=0;fallback[i]=0;
        xyz[3*i]=NAN;xyz[3*i+1]=NAN;xyz[3*i+2]=NAN;
        if(!accepted[i]) continue;
        /* OpenCV's zero-distortion normalization uses reciprocal focal lengths. */
        x=(q[i]/4096.-p[2])*p[0];y=(rows[i]-p[3])*p[1];
        if(!isfinite(x)||!isfinite(y)) {fallback[i]=1;continue;}
        scale=fmax(1.,fmax(fabs(x),fabs(y)));
        sx=x/scale;sy=y/scale;sz=1./scale;
        length=sqrt((sx*sx+sy*sy)+sz*sz);
        dx=sx/length;dy=sy/length;dz=sz/length;
        cosine=(dx*p[4]+dy*p[5])+dz*p[6];
        if(!isfinite(cosine)||!isfinite(dz)||dz<=0.) {fallback[i]=1;continue;}
        /* Grazing intersections amplify reduction ULPs; preserve frozen math. */
        if(fabs(cosine)<.05) fallback[i]=1;
        if(fabs(fabs(cosine)-floor)<=1e-12) fallback[i]=1;
        distance=-p[7]/cosine;
        if(isfinite(distance)&&fabs(distance)<=1e-12) fallback[i]=1;
        if(fabs(cosine)<=floor) {reason[i]=2;continue;}
        if(!isfinite(distance)||distance<=0.) {reason[i]=3;continue;}
        if(has_limits) {
            tolerance=1e-9*fmax(1.,fabs(distance));
            if(fabs(distance-p[8])<=tolerance||fabs(distance-p[9])<=tolerance) fallback[i]=1;
            if(distance<p[8]||distance>p[9]) {reason[i]=4;continue;}
        }
        xyz[3*i]=distance*dx;xyz[3*i+1]=distance*dy;xyz[3*i+2]=distance*dz;
        if(!isfinite(xyz[3*i])||!isfinite(xyz[3*i+1])||!isfinite(xyz[3*i+2])) {
            fallback[i]=1;continue;
        }
        /* Keep absolute XYZ agreement conservative outside scanner scale. */
        if(fabs(xyz[3*i])>10000.||fabs(xyz[3*i+1])>10000.||fabs(xyz[3*i+2])>10000.) fallback[i]=1;
        if(xyz[3*i+2]<=0.) {
            reason[i]=3;xyz[3*i]=NAN;xyz[3*i+1]=NAN;xyz[3*i+2]=NAN;continue;
        }
        reason[i]=1;
        for(j=0;j<3;++j) {
            double hit=xyz[3*i+j],lo=p[10+j],hi=p[13+j];
            if(hit<lo||hit>hi) outside[i]=1;
            if(fabs(hit-lo)<=1e-8+1e-9*fabs(lo)||fabs(hit-hi)<=1e-8+1e-9*fabs(hi)) fallback[i]=1;
        }
    }
}
