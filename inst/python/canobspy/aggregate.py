"""
Lighter versions of a series of orthomosaics: aggregated to a coarse resolution (0.5 m by default), with optional
bands of RGB spectral indices.

Each index is computed on the pixels of the mosaic (e.g. 5 cm), then averaged in each cell of the coarse grid, like
the R, G and B bands: the mean of the index of the pixels, not the index of the mean colour. The cells are those of a
grid aligned on multiples of the resolution, so that the mosaics of all dates (and of later runs) overlay exactly.
"""
import math
import os
import time

import numpy as np

# name: (function of R, G, B (float32 arrays of digital numbers), formula, reference)
INDICES = {
    'GCC':   (lambda R, G, B: G / (R + G + B), 'G / (R + G + B)', 'Richardson et al. 2007'),
    'RCC':   (lambda R, G, B: R / (R + G + B), 'R / (R + G + B)', 'Richardson et al. 2007'),
    'BCC':   (lambda R, G, B: B / (R + G + B), 'B / (R + G + B)', 'Richardson et al. 2007'),
    'ExG':   (lambda R, G, B: (2 * G - R - B) / (R + G + B), '2g - r - b (chromatic coordinates)', 'Woebbecke et al. 1995'),
    'ExR':   (lambda R, G, B: (1.4 * R - G) / (R + G + B), '1.4r - g (chromatic coordinates)', 'Meyer et al. 1998'),
    'ExGR':  (lambda R, G, B: (3 * G - 2.4 * R - B) / (R + G + B), 'ExG - ExR', 'Meyer & Neto 2008'),
    'GLI':   (lambda R, G, B: (2 * G - R - B) / (2 * G + R + B), '(2G - R - B) / (2G + R + B)', 'Louhaichi et al. 2001'),
    'VARI':  (lambda R, G, B: np.clip((G - R) / (G + R - B), -1, 1), '(G - R) / (G + R - B), bounded to [-1, 1]',
              'Gitelson et al. 2002'),
    'NGRDI': (lambda R, G, B: (G - R) / (G + R), '(G - R) / (G + R)', 'Tucker 1979'),
    'NDYI':  (lambda R, G, B: (G - B) / (G + B), '(G - B) / (G + B)', 'Sulik & Long 2016'),
    'MGRVI': (lambda R, G, B: (G * G - R * R) / (G * G + R * R), '(G^2 - R^2) / (G^2 + R^2)', 'Bendig et al. 2015'),
    'RGBVI': (lambda R, G, B: (G * G - R * B) / (G * G + R * B), '(G^2 - R B) / (G^2 + R B)', 'Bendig et al. 2015'),
    'TGI':   (lambda R, G, B: G - 0.39 * R - 0.61 * B, 'G - 0.39 R - 0.61 B', 'Hunt et al. 2013'),
    'CIVE':  (lambda R, G, B: 0.441 * R - 0.811 * G + 0.385 * B + 18.78745, '0.441 R - 0.811 G + 0.385 B + 18.787',
              'Kataoka et al. 2003'),
    'VEG':   (lambda R, G, B: G / (np.power(R, 0.667) * np.power(B, 0.333)), 'G / (R^0.667 B^0.333)', 'Hague et al. 2006'),
    'BI':    (lambda R, G, B: np.sqrt((R * R + G * G + B * B) / 3), 'sqrt((R^2 + G^2 + B^2) / 3)', 'brightness'),
    'SAT':   (lambda R, G, B: (np.maximum(np.maximum(R, G), B) - np.minimum(np.minimum(R, G), B))
              / np.maximum(np.maximum(R, G), B), '(max - min) / max of R, G, B', 'saturation (HSV)'),
}

GTIFF_OPTIONS = ['COMPRESS=LZW', 'PREDICTOR=3', 'TILED=YES', 'BIGTIFF=IF_SAFER']


def list_indices():
    """Available indices: list of dicts (name, formula, reference)."""
    return [{'name': k, 'formula': v[1], 'reference': v[2]} for k, v in INDICES.items()]


def _check_indices(indices):
    indices = list(indices or [])
    bad = [i for i in indices if i not in INDICES]
    if bad:
        raise ValueError(f'Unknown index: {", ".join(bad)}. Available: {", ".join(INDICES)}')
    return indices


def snapped_extent(paths, res):
    """Union of the extents of rasters, enlarged to multiples of res: (xmin, ymin, xmax, ymax)."""
    from osgeo import gdal
    xs, ys = [], []
    for p in paths:
        ds = gdal.Open(p)
        gt = ds.GetGeoTransform()
        xs += [gt[0], gt[0] + gt[1] * ds.RasterXSize]
        ys += [gt[3], gt[3] + gt[5] * ds.RasterYSize]
        ds = None
    return (math.floor(min(xs) / res) * res, math.floor(min(ys) / res) * res,
            math.ceil(max(xs) / res) * res, math.ceil(max(ys) / res) * res)


def aggregate_mosaic(src, dst, res=0.5, indices=None, extent=None, min_coverage=0.5, background=(255, 0),
                     strip_rows=2048):
    """
    Writes `dst`: the mosaic `src` aggregated to `res` metres (mean of the pixels of each cell), bands R, G, B and
    the indices requested, float32, no data = NaN. extent: (xmin, ymin, xmax, ymax) of the output grid (multiples
    of res), default: the extent of src. Cells covered by less than min_coverage of mosaic pixels are no data;
    pixels whose 3 bands are all equal to a `background` value (white 255, black 0) are not mosaic pixels.
    """
    from osgeo import gdal
    gdal.UseExceptions()
    indices = _check_indices(indices)
    ds = gdal.Open(src)
    gt = ds.GetGeoTransform()
    if abs(gt[2]) > 0 or abs(gt[4]) > 0:
        raise ValueError(f'{src}: rotated rasters are not supported')
    W, H = ds.RasterXSize, ds.RasterYSize
    alpha = ds.RasterCount >= 4 and ds.GetRasterBand(4).GetColorInterpretation() == gdal.GCI_AlphaBand
    if extent is None:
        extent = snapped_extent([src], res)
    xmin, ymin, xmax, ymax = extent
    nc, nr = int(round((xmax - xmin) / res)), int(round((ymax - ymin) / res))
    nb = 3 + len(indices)
    sums = np.zeros((nb, nr * nc), np.float32)
    cnt = np.zeros((nb, nr * nc), np.uint16 if (res / abs(gt[1])) ** 2 < 6e4 else np.uint32)
    # target column of each source column, target row of each source row (cell containing the pixel centre)
    col = np.floor((gt[0] + (np.arange(W) + 0.5) * gt[1] - xmin) / res).astype(np.int64)
    row = np.floor((ymax - (gt[3] + (np.arange(H) + 0.5) * gt[5])) / res).astype(np.int64)
    okc = (col >= 0) & (col < nc)
    for r0 in range(0, H, strip_rows):
        h = min(strip_rows, H - r0)
        arr = ds.ReadAsArray(0, r0, W, h)
        R, G, B = (arr[i].astype(np.float32) for i in range(3))
        valid = np.ones((h, W), bool)
        for v in background:
            valid &= ~((arr[0] == v) & (arr[1] == v) & (arr[2] == v))
        if alpha:
            valid &= arr[3] > 0
        rr = row[r0:r0 + h]
        valid &= ((rr >= 0) & (rr < nr))[:, None] & okc[None, :]
        if not valid.any():
            continue
        cell = (rr[:, None] * nc + col[None, :])[valid]
        lo = int(cell.min())
        cell -= lo
        n = int(cell.max()) + 1
        bands = [R, G, B]
        with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
            bands += [INDICES[i][0](R, G, B) for i in indices]
        for b, x in enumerate(bands):
            x = x[valid]
            fin = np.isfinite(x)
            sums[b, lo:lo + n] += np.bincount(cell[fin], weights=x[fin], minlength=n).astype(np.float32)
            cnt[b, lo:lo + n] += np.bincount(cell[fin], minlength=n).astype(cnt.dtype)
    ds = None
    full = (res / abs(gt[1])) * (res / abs(gt[5]))       # mosaic pixels in a full cell
    with np.errstate(divide='ignore', invalid='ignore'):
        out = sums / cnt
    out[cnt < max(1, min_coverage * full)] = np.nan
    out[:, cnt[0] < max(1, min_coverage * full)] = np.nan   # a cell outside the mosaic is empty in all bands
    tmp = dst + '.tmp.tif'
    o = gdal.GetDriverByName('GTiff').Create(tmp, nc, nr, nb, gdal.GDT_Float32, GTIFF_OPTIONS)
    o.SetGeoTransform((xmin, res, 0, ymax, 0, -res))
    o.SetProjection(gdal.Open(src).GetProjection())
    for b, name in enumerate(['R', 'G', 'B'] + indices):
        band = o.GetRasterBand(b + 1)
        band.WriteArray(out[b].reshape(nr, nc))
        band.SetNoDataValue(float('nan'))
        band.SetDescription(name)
    o = None
    os.replace(tmp, dst)
    return {'cells': int(np.isfinite(out[0]).sum()), 'hectares': round(float(np.isfinite(out[0]).sum()) * res * res / 1e4, 2)}


def _label(res):
    return f'{res * 100:g}cm' if res < 1 else f'{res:g}m'


def aggregate_series(mosaics_dir, out_dir, resolution=0.5, indices=None, common_extent=True, min_coverage=0.5,
                     background=(255, 0), overwrite=False, epochs=None, suffix=None):
    """
    aggregate_mosaic() for all the .tif of mosaics_dir: out_dir/<name>_<suffix>.tif (suffix: '50cm' for 0.5 m).
    common_extent: all the outputs on the same grid (union of the extents), directly stackable.
    Returns a list of dicts (file, output, status, hectares, seconds).
    """
    t_start = time.time()
    indices = _check_indices(indices)
    os.makedirs(out_dir, exist_ok=True)
    files = sorted(f for f in os.listdir(mosaics_dir) if f.lower().endswith(('.tif', '.tiff')))
    if epochs:
        files = [f for f in files if os.path.splitext(f)[0] in set(epochs)]
    if not files:
        raise ValueError(f'No .tif in {mosaics_dir}')
    suffix = suffix or _label(resolution)
    paths = [os.path.join(mosaics_dir, f) for f in files]
    extent = snapped_extent(paths, resolution) if common_extent else None
    print(f'{len(files)} mosaics -> {out_dir}: {resolution} m, bands R, G, B'
          + (', ' + ', '.join(indices) if indices else '')
          + (f'; common grid {extent[2] - extent[0]:.0f} x {extent[3] - extent[1]:.0f} m' if extent else ''), flush=True)
    rows, durations = [], []
    for k, (f, p) in enumerate(zip(files, paths), 1):
        out = os.path.join(out_dir, f'{os.path.splitext(f)[0]}_{suffix}.tif')
        if os.path.isfile(out) and not overwrite:
            rows.append({'file': p, 'output': out, 'status': 'exists', 'hectares': None, 'seconds': 0})
            continue
        t0 = time.time()
        try:
            st = aggregate_mosaic(p, out, resolution, indices, extent, min_coverage, background)
            durations.append(time.time() - t0)
            left = sum(durations) / len(durations) * (len(files) - k)
            print(f'[{k}/{len(files)}] {os.path.basename(out)}: {st["hectares"]} ha, {time.time() - t0:.0f} s'
                  f'; remaining about {left / 60:.0f} min', flush=True)
            rows.append({'file': p, 'output': out, 'status': 'done', 'hectares': st['hectares'],
                         'seconds': round(time.time() - t0)})
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - reported, next mosaic
            print(f'[{k}/{len(files)}] {f}: FAILED: {type(e).__name__}: {e}', flush=True)
            rows.append({'file': p, 'output': out, 'status': 'failed', 'hectares': None,
                         'seconds': round(time.time() - t0), 'message': f'{type(e).__name__}: {e}'})
    n_ok = sum(r['status'] in ('done', 'exists') for r in rows)
    print(f'{n_ok}/{len(files)} aggregated mosaics in {out_dir} ({(time.time() - t_start) / 60:.0f} min for this run)',
          flush=True)
    return rows
