# Comparison of two series of orthomosaics (e.g. Metashape vs ODM) with AROSICS local co-registration.
#
# Used by compare_Mosa_engines.R. AROSICS settings are the ones of canobsR align_Mosa() (local mode):
# matching window 256 px, max_shift 250 px, max_iter 5, min_reliability 60 %, grid of about 25 x 25 points.
#
# For a pair (reference, target), AROSICS measures on a grid of points the local shift of the target relative
# to the reference (X_SHIFT_M, Y_SHIFT_M, in map units). Points that failed or were flagged as outliers by the
# AROSICS tie point filtering are excluded from the statistics.

import os
import sys
import re
import glob
import time

import numpy as np

FILL = -9999


def series(folder):
    """{date: path} of the .tif of a folder, the date (YYYYMMDD, else YYYYMM) being read in the file name."""
    out = {}
    for f in sorted(glob.glob(os.path.join(folder, '*.tif'))):
        m = re.search(r'(?<!\d)(\d{8})(?!\d)', os.path.basename(f)) or re.search(r'(?<!\d)(\d{6})(?!\d)', os.path.basename(f))
        if m and m.group(1) not in out:
            out[m.group(1)] = f
    return out


def _grid_res(path):
    from osgeo import gdal
    ds = gdal.Open(path)
    g = int(max(ds.RasterXSize, ds.RasterYSize) // 25)
    ds = None
    return g


def on_ref_grid(ref, tgt, out_vrt, nodata=255):
    """
    Target as a virtual raster (VRT, no copy of the pixels) with its 3 first bands, resampled (bilinear) on the
    pixel grid of the reference when the grids differ (crs, resolution or origin). AROSICS can then work on
    identical grids (some versions of its dependencies fail when they have to resample a nodata mask).
    """
    from osgeo import gdal
    r, t = gdal.Open(ref), gdal.Open(tgt)
    gr, gt = r.GetGeoTransform(), t.GetGeoTransform()
    same = (r.GetProjection() == t.GetProjection() and abs(gr[1] - gt[1]) < 1e-9 and abs(gr[5] - gt[5]) < 1e-9
            and abs(((gt[0] - gr[0]) / gr[1]) % 1) < 1e-6 and abs(((gt[3] - gr[3]) / gr[5]) % 1) < 1e-6)
    nb = t.RasterCount
    if same and nb <= 3:
        return tgt
    bands = gdal.Translate(out_vrt + '_bands.vrt', t, format='VRT', bandList=list(range(1, min(3, nb) + 1)))
    bands = None
    if same:
        return out_vrt + '_bands.vrt'
    xmin, ymax = gr[0], gr[3]
    xmax, ymin = xmin + gr[1] * r.RasterXSize, ymax + gr[5] * r.RasterYSize
    gdal.Warp(out_vrt, out_vrt + '_bands.vrt', format='VRT', dstSRS=r.GetProjection(),
              outputBounds=(xmin, ymin, xmax, ymax), xRes=gr[1], yRes=abs(gr[5]), resampleAlg='bilinear',
              srcNodata=nodata, dstNodata=nodata)
    return out_vrt


def _n_cpus(CPUs=None):
    """
    Number of processes for AROSICS. On Windows, a multiprocessing pool cannot wait on more than 63 handles (about 61
    processes): with more (e.g. 64 threads), its internal thread fails ("need at most 63 handles") and the pool
    hangs without an error in R. The number is therefore capped at 60 on Windows (16 by default).
    """
    # default: 16 processes. On Windows each AROSICS process receives its own copy of the images (no fork):
    # with 60 processes and orthomosaics of ~15000 x 14000 px, the memory is exhausted and the run seems frozen.
    n = min(os.cpu_count() or 1, 16) if CPUs is None else int(CPUs)
    if sys.platform.startswith('win'):
        n = min(n, 60)
    return max(1, n)


def coreg_pair(ref, tgt, out_prefix, grid_res=None, window_size=256, max_shift=250, max_iter=5,
               min_reliability=60, band=1, nodata=255, CPUs=None):
    """
    Local co-registration of tgt on ref. Writes <out_prefix>_points.csv (all grid points) and
    <out_prefix>_vectors.png, and returns a dict of statistics on the valid points.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from arosics import COREG_LOCAL

    tgt_file = tgt
    tgt = on_ref_grid(ref, tgt, out_prefix + '_target.vrt', nodata)
    if grid_res is None:
        grid_res = _grid_res(tgt)
    t0 = time.time()
    from .resources import with_fewer_processes

    def run(k):
        c = COREG_LOCAL(ref, tgt, grid_res=int(grid_res), window_size=(int(window_size), int(window_size)),
                        max_shift=int(max_shift), max_iter=int(max_iter), min_reliability=float(min_reliability),
                        r_b4match=int(band), s_b4match=int(band), nodata=(nodata, nodata),
                        CPUs=k, q=True, progress=False)
        c.calculate_spatial_shifts()
        return c
    crl = with_fewer_processes(run, _n_cpus(CPUs))
    tbl = crl.CoRegPoints_table
    n_grid = len(tbl)
    keep_cols = [c for c in ('POINT_ID', 'X_MAP', 'Y_MAP', 'X_SHIFT_M', 'Y_SHIFT_M', 'ABS_SHIFT', 'RELIABILITY',
                             'SSIM_BEFORE', 'SSIM_AFTER', 'OUTLIER') if c in tbl.columns]
    tbl = tbl[keep_cols].copy()
    tbl.to_csv(out_prefix + '_points.csv', index=False)

    ok = (tbl['X_SHIFT_M'] != FILL) & (tbl['Y_SHIFT_M'] != FILL)
    if 'OUTLIER' in tbl.columns:
        ok &= ~tbl['OUTLIER'].astype(str).str.lower().isin(['true', '1'])
    v = tbl[ok]
    dx, dy = v['X_SHIFT_M'].to_numpy(float), v['Y_SHIFT_M'].to_numpy(float)
    mag = np.hypot(dx, dy)
    res = {'reference': os.path.basename(ref), 'target': os.path.basename(tgt_file),
           'n_grid_points': int(n_grid), 'n_valid': int(ok.sum()),
           'pct_valid': round(100.0 * ok.sum() / n_grid, 1) if n_grid else 0.0,
           'median_dx_m': float(np.median(dx)) if len(dx) else np.nan,
           'median_dy_m': float(np.median(dy)) if len(dy) else np.nan,
           'median_abs_m': float(np.median(mag)) if len(mag) else np.nan,
           'p90_abs_m': float(np.percentile(mag, 90)) if len(mag) else np.nan,
           'max_abs_m': float(mag.max()) if len(mag) else np.nan,
           # local variability around the median shift (distortion between the two mosaics)
           'median_dev_from_median_m': float(np.median(np.hypot(dx - np.median(dx), dy - np.median(dy)))) if len(dx) else np.nan,
           'mean_reliability': float(v['RELIABILITY'].mean()) if len(v) and 'RELIABILITY' in v else np.nan,
           'seconds': round(time.time() - t0, 1)}

    # vector map
    fig, ax = plt.subplots(figsize=(8, 8))
    if len(v):
        q = ax.quiver(v['X_MAP'], v['Y_MAP'], dx, dy, mag, cmap='viridis', angles='xy', scale_units='xy',
                      scale=max(mag.max(), 1e-6) / (grid_res * _pixel_size(tgt) * 0.9))
        plt.colorbar(q, ax=ax, label='shift (m)', shrink=0.7)
    bad = tbl[~ok]
    ax.scatter(bad['X_MAP'], bad['Y_MAP'], s=4, c='lightgrey', label='no valid shift')
    ax.set_aspect('equal')
    ax.set_title(f"{os.path.basename(tgt_file)}\nvs {os.path.basename(ref)}\n"
                 f"median |shift| {res['median_abs_m']:.3f} m, p90 {res['p90_abs_m']:.3f} m, {res['n_valid']} points",
                 fontsize=9)
    ax.legend(loc='lower right', fontsize=7)
    fig.tight_layout()
    fig.savefig(out_prefix + '_vectors.png', dpi=110)
    plt.close(fig)
    for f in (out_prefix + '_target.vrt', out_prefix + '_target.vrt_bands.vrt'):
        if os.path.exists(f):
            os.remove(f)
    return res


def _pixel_size(path):
    from osgeo import gdal
    ds = gdal.Open(path)
    p = abs(ds.GetGeoTransform()[1])
    ds = None
    return p
