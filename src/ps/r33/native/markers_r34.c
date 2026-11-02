/* Display-only R33 marker sampling. No mutable global workspace or pixel work. */
#include <stdint.h>
#include <stddef.h>
#include <limits.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif

static unsigned group_of(uint32_t flags, uint32_t decisions)
{
    unsigned measurement = (flags & 2u) && (decisions & 1u);
    unsigned preview = (flags & 4u) && (decisions & 2u) && !measurement;
    return measurement ? 0u : (preview ? 1u : 2u);
}

/* out holds capacity rows of three int64: x_q12, pixel_y, uncomputed_start. */
API int r34_markers(const uint32_t *records, uint64_t n,
                    uint32_t decisions, uint32_t limit, int64_t *out,
                    uint32_t capacity, uint32_t *counts)
{
    uint64_t sizes[3] = {0, 0, 0}, seen[3] = {0, 0, 0};
    uint64_t wanted[3] = {0, 0, 0};
    uint32_t quotas[3] = {0, 0, 0}, emitted[3] = {0, 0, 0};
    uint32_t offsets[3], remaining;
    uint64_t i;
    unsigned g;
    if ((!records && n) || !out || !counts || !limit || limit > 512u ||
            capacity < limit || n > SIZE_MAX / (16u * sizeof(uint32_t)))
        return 3;
    for (i = 0; i < n; ++i) {
        const uint32_t *a = records + (size_t)i * 16u;
        uint32_t flags = a[13] >> 24;
        g = group_of(flags, decisions);
        if (g < 2u && !(flags & 1u)) return 1;
        ++sizes[g];
    }
    remaining = n < limit ? (uint32_t)n : limit;
    while (remaining) {
        uint32_t active = 0, rounds;
        for (g = 0; g < 3; ++g) if (quotas[g] < sizes[g]) ++active;
        if (!active) return 3;
        rounds = remaining / active;
        for (g = 0; g < 3; ++g) if (quotas[g] < sizes[g]) {
            uint64_t left = sizes[g] - quotas[g];
            if (left < rounds) rounds = (uint32_t)left;
        }
        if (rounds) {
            for (g = 0; g < 3; ++g) if (quotas[g] < sizes[g]) quotas[g] += rounds;
            remaining -= rounds * active;
        } else {
            for (g = 0; g < 3 && remaining; ++g) if (quotas[g] < sizes[g]) {
                ++quotas[g]; --remaining;
            }
        }
    }
    offsets[0] = 0; offsets[1] = quotas[0]; offsets[2] = quotas[0] + quotas[1];
    for (i = 0; i < n; ++i) {
        const uint32_t *a = records + (size_t)i * 16u;
        uint32_t flags = a[13] >> 24;
        uint64_t ordinal, u, v;
        int64_t *dst;
        unsigned valid = (flags & 1u) != 0;
        g = group_of(flags, decisions);
        ordinal = seen[g]++;
        if (emitted[g] == quotas[g] || ordinal != wanted[g]) continue;
        u = valid ? ((uint64_t)a[4] + 2048u) / 4096u : a[2];
        v = valid ? ((uint64_t)a[5] + 2048u) / 4096u : a[1];
        if (u > 752u || v > 480u) return 2;
        dst = out + ((size_t)offsets[g] + emitted[g]) * 3u;
        dst[0] = (int64_t)(u > 751u ? 751u : u) * 4096;
        dst[1] = (int64_t)(v > 479u ? 479u : v);
        dst[2] = !valid;
        ++emitted[g];
        if (emitted[g] < quotas[g]) {
            uint64_t div = quotas[g] > 1u ? quotas[g] - 1u : 1u;
            /* Quotient/remainder formulation cannot overflow n*sample_index. */
            uint64_t span = sizes[g] - 1u;
            wanted[g] = (span / div) * emitted[g] +
                        ((span % div) * emitted[g]) / div;
        }
    }
    for (g = 0; g < 3; ++g) {
        if (emitted[g] != quotas[g]) return 3;
        counts[g] = quotas[g];
    }
    return 0;
}
