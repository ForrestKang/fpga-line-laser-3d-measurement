/* Wire observations -> numeric columns only. No pixel/acceptance algorithm. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

uint32_t observation_columns_r28_abi(void) { return 0x280001u; }

static uint32_t word(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1]<<8) |
           ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24);
}

int observation_columns_r28(const void *records, size_t n,
                            int64_t *integers, double *floating) {
    if (n>7680 || (n && (!records || !integers || !floating))) return 1;
    const unsigned char *p=(const unsigned char *)records;
    for (size_t i=0;i<n;i++,p+=64) {
        const uint32_t start=word(p+8), end=word(p+12);
        integers[i]=(int64_t)word(p+4);
        integers[n+i]=(int64_t)word(p+16);
        integers[2*n+i]=(int64_t)end-(int64_t)start+1;
        integers[3*n+i]=(int64_t)(word(p+60)>>24);
        integers[4*n+i]=(int64_t)start;
        integers[5*n+i]=(int64_t)end;
        integers[6*n+i]=(int64_t)word(p);
        floating[i]=(double)word(p+40)/4096.0;
        floating[n+i]=(double)word(p+44)/4096.0;
    }
    return 0;
}
