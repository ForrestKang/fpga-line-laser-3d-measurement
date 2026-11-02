/* One full diagnostic-record pass. Wire conversion only, never admission. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

uint32_t observation_modes_r29_abi(void) { return 0x290001u; }

static uint32_t word(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1]<<8) |
           ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24);
}

int observation_modes_r29(const void *records, size_t n, size_t qualified,
                         int64_t *integer, double *floating, uint8_t *modes,
                         uint64_t *counts, uint32_t *accepted) {
    if (n>180480u || qualified>7680u || qualified>n || !counts || !accepted ||
        (n && !records) || (qualified && (!integer || !floating || !modes))) return 1;
    memset(counts,0,16*sizeof(*counts));
    accepted[0]=accepted[1]=0;
    const unsigned char *p=(const unsigned char *)records;
    size_t out=0;
    for (size_t i=0;i<n;i++,p+=64) {
        const uint32_t decisions=word(p+52);
        const uint32_t local=decisions&255u;
        const uint32_t measure=(decisions>>8)&255u, preview=(decisions>>16)&255u;
        /* local is a detector rejection BITMASK, not a mode reason code. */
        if (measure>7u || preview>7u) return 2;
        counts[measure]++; counts[8+preview]++;
        if (local) continue;
        if (out>=qualified) return 3;
        const uint32_t start=word(p+8), end=word(p+12), branches=word(p+56);
        const uint32_t mb=branches&65535u, pb=branches>>16;
        integer[out]=(int64_t)word(p+4);
        integer[qualified+out]=(int64_t)word(p+16);
        integer[2*qualified+out]=(int64_t)end-(int64_t)start+1;
        integer[3*qualified+out]=(int64_t)(word(p+60)>>24);
        integer[4*qualified+out]=(int64_t)start;
        integer[5*qualified+out]=(int64_t)end;
        integer[6*qualified+out]=(int64_t)word(p);
        integer[7*qualified+out]=mb==65535u ? -1 : (int64_t)mb;
        integer[8*qualified+out]=pb==65535u ? -1 : (int64_t)pb;
        floating[out]=(double)word(p+40)/4096.0;
        floating[qualified+out]=(double)word(p+44)/4096.0;
        modes[out]=(uint8_t)((decisions>>25)&1u);
        modes[qualified+out]=(uint8_t)((decisions>>26)&1u);
        modes[2*qualified+out]=(uint8_t)measure;
        modes[3*qualified+out]=(uint8_t)preview;
        accepted[0]+=modes[out]; accepted[1]+=modes[qualified+out];
        out++;
    }
    return out==qualified ? 0 : 3;
}
