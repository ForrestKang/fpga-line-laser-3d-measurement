/* Pixel raster only. No allocation, retained pointers, Python API or hardware. */
#include <stdint.h>
#include <stddef.h>
#include <limits.h>
#include <math.h>
#include <fenv.h>
#if defined(__SSE__) || defined(__x86_64__)
#include <xmmintrin.h>
#endif

#if defined(_WIN32)
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

EXPORT uint32_t sl_point_raster_abi(void) { return 1u; }

/* Return 1 for unsupported rounding, 2 for nonfinite/range, 3 for bad ABI args.
 * Scratch may change on failure; panel cannot change until all rows validate.
 * Python retains min/max, centering, basis, projections and scale computation.
 */
EXPORT int sl_point_raster_v1(
    const double *xyz, const double *projected_x, const double *projected_y,
    size_t count, double scale, double z_min, double z_span,
    int32_t width, int32_t height, uint8_t *panel, size_t panel_stride,
    int32_t *screen_x, int32_t *screen_y, uint8_t *colors, size_t capacity)
{
    size_t i;
    int dy;
    if (fegetround() != FE_TONEAREST) return 1;
#if defined(__SSE__) || defined(__x86_64__)
    /* Some Windows CRTs expose only one of x87/SSE through fegetround. */
    if ((_mm_getcsr() & 0x6000u) != 0u) return 1;
#endif
    if (width < 7 || height < 7 || count > capacity ||
        (size_t)width > SIZE_MAX / 3u || panel_stride < (size_t)width * 3u ||
        (size_t)height > SIZE_MAX / panel_stride || count > SIZE_MAX / 3u ||
        !xyz || !projected_x || !projected_y || !panel ||
        !screen_x || !screen_y || !colors) return 3;
    if (!isfinite(scale) || !isfinite(z_min) || !isfinite(z_span) || z_span <= 0.0)
        return 2;
    for (i = 0; i < count; ++i) {
        double x, y, z, sx, sy, ratio, b, g, r;
        if (!isfinite(xyz[3u*i]) || !isfinite(xyz[3u*i+1u]) ||
            !isfinite(xyz[3u*i+2u]) || !isfinite(projected_x[i]) ||
            !isfinite(projected_y[i])) return 2;
        x = projected_x[i] * scale;
        y = projected_y[i] * scale;
        x = (double)width * 0.52 + x;
        y = (double)height * 0.48 - y;
        if (!isfinite(x) || !isfinite(y)) return 2;
        sx = nearbyint(x);
        sy = nearbyint(y);
        if (sx < (double)INT32_MIN || sx > (double)INT32_MAX ||
            sy < (double)INT32_MIN || sy > (double)INT32_MAX) return 2;
        screen_x[i] = (int32_t)sx;
        screen_y[i] = (int32_t)sy;
        z = xyz[3u*i+2u] - z_min;
        ratio = z / z_span;
        if (!isfinite(ratio)) return 2;
        if (ratio < 0.0) ratio = 0.0;
        if (ratio > 1.0) ratio = 1.0;
        b = 210.0 - 110.0 * ratio;
        g = 150.0 + 75.0 * ratio;
        r = 105.0 + 115.0 * ratio;
        colors[3u*i] = (uint8_t)b;
        colors[3u*i+1u] = (uint8_t)g;
        colors[3u*i+2u] = (uint8_t)r;
    }
    /* Preserve full dy=0 pass before full dy=1 pass, including collisions. */
    for (dy = 0; dy <= 1; ++dy) {
        for (i = 0; i < count; ++i) {
            int32_t x = screen_x[i], y = screen_y[i];
            if (x >= 3 && x < width - 3 && y >= 3 && y < height - 3) {
                uint8_t *pixel = panel + (size_t)(y + dy) * panel_stride + (size_t)x * 3u;
                pixel[0] = colors[3u*i];
                pixel[1] = colors[3u*i+1u];
                pixel[2] = colors[3u*i+2u];
            }
        }
    }
    return 0;
}
