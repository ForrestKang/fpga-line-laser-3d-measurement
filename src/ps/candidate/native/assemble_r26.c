/* Native PFO3 primary-stream validation + patch reassembly.
 *
 * Same rules as candidate/pfo3_stream_r25.py:assemble(); one call replaces the
 * ~40 small numpy operations that dominate on ARM. Any rule violation returns a
 * non-zero code and the Python caller raises the same error text as before.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>

#define HEADER_MAGIC 0x33544850u
#define FOOTER_MAGIC 0x33544650u
#define TAG_BASE     0x33520000u
#define OUT_HEADER_WORDS 64
/* Contract cap: ceil(752/2)*480 = 180480 diagnostics per frame. */
#define MAX_RECORDS 180480
#define MAX_PATCHES MAX_RECORDS

enum {
    E_OK = 0, E_TRUNCATED = 1, E_NOT_NATIVE = 2, E_SEQ = 3, E_HWERR = 4,
    E_RECORD = 5, E_TAG = 6, E_KIND = 7, E_COUNT = 8, E_ID = 9,
    E_PATCH_ID = 10, E_PATCH_SET = 11, E_PATCH_FLAG = 12, E_CAPACITY = 13
};

/* column indices inside the 16-word local record */
#define L_ROW 1
#define L_DECISIONS 13
#define L_BRANCH 14
#define L_AUX 15

static int cmp_u32(const void* a, const void* b) {
    uint32_t x = *(const uint32_t*)a, y = *(const uint32_t*)b;
    return (x > y) - (x < y);
}

int assemble_r26(const uint8_t* primary, uint32_t primary_bytes,
                 uint8_t* out, uint32_t out_capacity, uint32_t* out_bytes)
{
    /* The ctypes caller always supplies these, but a null check keeps the
     * exported ABI safe for ASan/fuzz harnesses and future callers. */
    if (primary == NULL || out == NULL || out_bytes == NULL) return E_TRUNCATED;
    if (primary_bytes < 384u || (primary_bytes & 3u)) return E_TRUNCATED;
    const uint32_t* w = (const uint32_t*)primary;
    uint32_t nwords = primary_bytes / 4u;
    const uint32_t* start = w;
    const uint32_t* end = w + nwords - 32u;
    if (start[0] != HEADER_MAGIC || start[1] != 1u || end[0] != FOOTER_MAGIC || end[1] != 1u)
        return E_NOT_NATIVE;
    if (start[4] != end[23] || end[24] != nwords) return E_SEQ;
    if (end[17] != 0u) return E_HWERR;
    if ((nwords - 96u) % 17u) return E_RECORD;

    uint32_t rows_total = (nwords - 96u) / 17u;
    const uint32_t* tagged = w + 64u;
    uint32_t n_local = 0, n_patch = 0, n_row = 0;
    for (uint32_t i = 0; i < rows_total; i++) {
        uint32_t tag = tagged[i * 17u];
        if ((tag & 0xFFFFFFFCu) != TAG_BASE) return E_TAG;
        uint32_t kind = tag & 3u;
        if (kind == 3u) return E_KIND;
        if (kind == 0u) n_local++; else if (kind == 1u) n_patch++; else n_row++;
    }
    if (n_local != end[2] || n_row != start[3]) return E_COUNT;
    if (n_local > MAX_RECORDS || n_patch > MAX_PATCHES || n_row > 480u) return E_CAPACITY;

    /* copy local records into a working buffer (they are patched below) */
    static uint32_t local[MAX_RECORDS * 16];
    static uint32_t holes[MAX_RECORDS];
    static uint32_t ids[MAX_PATCHES];
    static uint32_t expected[MAX_RECORDS];
    static uint32_t sort_a[MAX_PATCHES];
    static uint32_t sort_b[MAX_PATCHES];

    if (n_local * 16u > MAX_RECORDS * 16u) return E_CAPACITY;
    uint32_t li = 0;
    for (uint32_t i = 0; i < rows_total; i++) {
        uint32_t kind = tagged[i * 17u] & 3u;
        if (kind != 0u) continue;
        memcpy(&local[li * 16u], &tagged[i * 17u + 1u], 16u * 4u);
        if (local[li * 16u] != li) return E_ID;
        li++;
    }

    /* qualified candidates and the first index of each row group */
    uint32_t n_q = 0;
    for (uint32_t i = 0; i < n_local; i++) {
        if ((local[i * 16u + L_DECISIONS] & 255u) == 0u) holes[n_q++] = i;
    }
    uint32_t n_expected = 0;
    for (uint32_t k = 0; k < n_q; k++) {
        uint32_t row = local[holes[k] * 16u + L_ROW];
        uint32_t slot = 0;
        for (uint32_t j = k; j > 0 && local[holes[j - 1] * 16u + L_ROW] == row; j--) slot++;
        if (slot < 16u) expected[n_expected++] = holes[k];
    }

    if (n_patch) {
        uint32_t np = 0;
        for (uint32_t i = 0; i < rows_total && np < n_patch; i++) {
            if ((tagged[i * 17u] & 3u) != 1u) continue;
            const uint32_t* p = &tagged[i * 17u + 1u];
            if (p[0] >= n_local) return E_PATCH_ID;
            ids[np++] = p[0];
            if (p[L_DECISIONS] & 255u) return E_PATCH_FLAG;
        }
        if (np != n_patch || n_patch != n_expected) return E_PATCH_SET;
        memcpy(sort_a, ids, n_patch * 4u);
        memcpy(sort_b, expected, n_expected * 4u);
        qsort(sort_a, n_patch, 4u, cmp_u32);
        qsort(sort_b, n_expected, 4u, cmp_u32);
        for (uint32_t i = 0; i < n_patch; i++) {
            if (sort_a[i] != sort_b[i]) return E_PATCH_SET;
            if (i && sort_a[i] == sort_a[i - 1]) return E_PATCH_ID;
        }
        /* apply the final decision/branch patches */
        for (uint32_t i = 0; i < rows_total; i++) {
            if ((tagged[i * 17u] & 3u) != 1u) continue;
            const uint32_t* p = &tagged[i * 17u + 1u];
            uint32_t id = p[0];
            local[id * 16u + L_DECISIONS] = (local[id * 16u + L_DECISIONS] & 255u) | (p[L_DECISIONS] & 0xFFFFFF00u);
            local[id * 16u + L_BRANCH] = p[L_BRANCH];
            local[id * 16u + L_AUX] = (local[id * 16u + L_AUX] & 0xFF000000u) | (p[L_AUX] & 0x00FFFFFFu);
        }
    } else if (n_expected) {
        return E_PATCH_SET;
    }

    uint32_t body_bytes = (n_local + n_row) * 64u;
    if (256u + body_bytes > out_capacity) return E_CAPACITY;
    uint32_t* hw = (uint32_t*)out;
    memcpy(hw, start, 64u * 4u);
    hw[0] = 0x334F4650u; hw[1] = 0x00030001u; hw[2] = 256u; hw[3] = 64u; hw[4] = 64u;
    hw[5] = 256u + body_bytes;
    hw[6] = n_local;
    hw[7] = end[3];
    hw[8] = start[3]; /* height (width,height = start[2:4]) */
    hw[9] = end[11];
    /* header word indices follow HEADER_FIELDS exactly */
    hw[42] = end[4];  hw[43] = end[5];      /* measurement_count, preview_count */
    hw[44] = end[10]; hw[45] = end[9];      /* sat255_count, sat250_count */
    hw[47] = end[14]; hw[48] = end[15];     /* processing cycles lo/hi */
    hw[50] = end[12]; hw[51] = end[13];     /* measure_status, preview_status */
    hw[52] = end[18]; hw[53] = end[19];
    hw[54] = end[20]; hw[55] = end[21];
    hw[56] = end[6];  hw[57] = end[7];      /* measure/preview valid rows */
    hw[58] = end[28]; hw[59] = end[22];     /* ambiguous_rows, raw_peak_median_x2 */
    hw[61] = end[17]; hw[62] = end[16];     /* error_code, input_bytes */
    hw[63] = end[25] | (end[26] << 1) | (end[27] << 8);

    uint8_t* body = out + 256u;
    memcpy(body, local, n_local * 64u);
    uint32_t off = n_local * 64u;
    for (uint32_t i = 0; i < rows_total; i++) {
        if ((tagged[i * 17u] & 3u) != 2u) continue;
        memcpy(body + off, &tagged[i * 17u + 1u], 64u);
        off += 64u;
    }
    hw[60] = (uint32_t)crc32(0L, body, body_bytes);
    *out_bytes = 256u + body_bytes;
    return E_OK;
}
