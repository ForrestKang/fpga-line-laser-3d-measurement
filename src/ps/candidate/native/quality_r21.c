/* Exact R21 FrameQuality reductions; image observations only, no geometry. */
#include <stdint.h>
#include <stdlib.h>
#include <math.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif
static int cmp_double(const void *a,const void *b) {
    const double x=*(const double *)a,y=*(const double *)b;
    return (x>y)-(x<y);
}
static double median(double *values,int n) {
    qsort(values,(size_t)n,sizeof(double),cmp_double);
    return n%2 ? values[n/2] : (values[n/2-1]+values[n/2])/2.;
}
/* thresholds: intensity,width,completeness,saturation,ambiguity,saturated rows.
   outputs: completeness,median intensity,width,saturation,ambiguity,
            valid rows,expected rows,saturated rows. Return original status. */
API int quality_reduce(const uint8_t *image,int height,int width,
    int x0,int y0,int x1,int y1,const int64_t *rows,const uint8_t *accepted,
    const double *candidate_width,const uint8_t *ambiguous,int n,
    const double *thresholds,double *out) {
    int h=y1-y0,w=x1-x0,status=0,valid_count=0,ambiguous_count=0,saturated_rows=0,wide=0;
    int64_t saturated_pixels=0;
    uint8_t *valid=NULL,*amb=NULL;
    double *widths=NULL,*peaks=NULL,*vi=NULL,*vw=NULL;
    if(!image || !thresholds || !out || height<1 || width<1 || x0<0 || y0<0 ||
       x1>width || y1>height || h<1 || w<1 || n<0) return -1;
    valid=(uint8_t *)calloc((size_t)h,1);amb=(uint8_t *)calloc((size_t)h,1);
    widths=(double *)calloc((size_t)h,sizeof(double));peaks=(double *)malloc((size_t)h*sizeof(double));
    vi=(double *)malloc((size_t)h*sizeof(double));vw=(double *)malloc((size_t)h*sizeof(double));
    if(!valid || !amb || !widths || !peaks || !vi || !vw) {status=-2;goto done;}
    for(int i=0;i<n;i++) {
        int64_t r=rows[i]-y0;
        if(r<0 || r>=h || !isfinite(candidate_width[i])) {status=-3;goto done;}
        if(accepted[i]) {
            valid[r]=1;
            if(candidate_width[i]>widths[r]) widths[r]=candidate_width[i];
        }
        if(ambiguous[i]) amb[r]=1;
    }
    for(int r=0;r<h;r++) {
        uint8_t peak=0;int saturated=0;
        const uint8_t *line=image+(size_t)(r+y0)*(size_t)width+x0;
        for(int c=0;c<w;c++) {
            const uint8_t value=line[c];
            if(value>peak) peak=value;
            if(value>=250) {saturated_pixels++;saturated=1;}
        }
        peaks[r]=(double)peak;saturated_rows+=saturated;ambiguous_count+=amb[r]!=0;
        if(valid[r]) {
            vi[valid_count]=(double)peak;vw[valid_count]=widths[r];valid_count++;
            if(widths[r]>thresholds[1]) wide=1;
        }
    }
    out[0]=(double)valid_count/h;out[1]=valid_count?median(vi,valid_count):NAN;
    out[2]=valid_count?median(vw,valid_count):NAN;
    out[3]=(double)saturated_pixels/((double)h*w);out[4]=(double)ambiguous_count/h;
    out[5]=(double)valid_count;out[6]=(double)h;out[7]=(double)saturated_rows;
    if(out[3]>thresholds[3] || (double)saturated_rows/h>thresholds[5]) status=1;
    else if(out[4]>thresholds[4] || wide) status=2;
    else if(!valid_count && median(peaks,h)<thresholds[0]) status=3;
    else if(out[0]<thresholds[2]) status=4;
    else if(valid_count && out[1]<thresholds[0]) status=3;
    else if(!valid_count) status=5;
 done:
    free(valid);free(amb);free(widths);free(peaks);free(vi);free(vw);
    return status;
}
