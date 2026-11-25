"""
Co-registration of a series of orthomosaics with AROSICS (local co-registration on a grid of tie points).

Each mosaic (target) is aligned on a reference, at its own resolution; the reference is first aggregated to a
coarse resolution (0.5 m by default) and converted to grey (mean of the RGB bands): at 5 cm the result is
practically the same, and the computation is many times faster. Three ways to choose the reference:

  previous  the last mosaic already aligned (in time) that covers enough of the site (min_ref_coverage of the
            largest mosaic); the first mosaic is the anchor of the series (or is aligned on `reference` if given);
  fixed     one raster for all the mosaics (an RGB mosaic, or a single-band raster such as a lidar model);
  nearest   in a folder of dated rasters (date YYYYMMDD in the file name): the one closest in time (before or after).

A journal (align_journal.csv) records each mosaic as soon as it is aligned: running align_series() again resumes
where it stopped, and retries the failed mosaics.
"""
import csv
import os
import re
import shutil
import sys
import time

DATE_RE = re.compile(r'(?<!\d)(\d{4})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)')
GTIFF_OPTIONS = ['COMPRESS=LZW', 'BIGTIFF=IF_SAFER', 'TILED=YES']
JOURNAL_FIELDS = ['epoch', 'date', 'file', 'reference', 'status', 'n_points', 'shift_x_m', 'shift_y_m',
                  'shift_max_m', 'coverage_ha', 'seconds', 'message']


def log(*a):
    print(*a, flush=True)


try:
    from osgeo import gdal as _gdal
    _gdal.UseExceptions()
except Exception:  # noqa: BLE001 - GDAL is checked by canobs_setup()
    pass


def n_cpus(n=None):
    """AROSICS processes: n, else min(CPUs, 16); at most 60 on Windows (limit of its process pool)."""
    c = os.cpu_count() or 1
    n = min(c, 16) if not n else int(n)
    return max(1, min(n, 60) if sys.platform == 'win32' else n)


def date_of(name):
    """YYYYMMDD found in a file name, None if absent."""
    m = DATE_RE.search(os.path.basename(name))
    return ''.join(m.groups()) if m else None


def list_rasters(folder):
    """Dated rasters of a folder, sorted by date then name: list of (date, epoch, path)."""
    out = []
    for f in sorted(os.listdir(folder)):
        if f.lower().endswith(('.tif', '.tiff')) and date_of(f):
            out.append((date_of(f), os.path.splitext(f)[0], os.path.join(folder, f)))
    return sorted(out)


def grey_reference(src, dst, res=0.5):
    """
    Writes the reference version of a raster: aggregated (mean) to res metres, one band (mean of the 3 first bands
    of an RGB raster; band 1 of a single-band raster). RGB: white background (255) = no data. Returns the area
    with data, in hectares.
    """
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(src)
    nb = ds.RasterCount
    b1 = ds.GetRasterBand(1)
    tmp = dst + '.tmp.tif'
    if nb >= 3:
        nodata = b1.GetNoDataValue()
        nodata = 255 if nodata is None else nodata
        gdal.Warp(tmp, ds, xRes=res, yRes=res, resampleAlg='average', srcNodata=nodata, dstNodata=nodata,
                  warpOptions=['UNIFIED_SRC_NODATA=YES'], creationOptions=GTIFF_OPTIONS,
                  srcAlpha=nb == 4 and ds.GetRasterBand(4).GetColorInterpretation() == gdal.GCI_AlphaBand)
        t = gdal.Open(tmp)
        arr = np.stack([t.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(3)])
        nd = t.GetRasterBand(1).GetNoDataValue()
        mask = np.any(arr == nd, axis=0) if nd is not None else np.zeros(arr.shape[1:], bool)
        grey = np.clip(np.round(arr.mean(axis=0)), 0, 254).astype(np.uint8)
        grey[mask] = 255
        out_nd, dtype = 255, gdal.GDT_Byte
    else:
        nodata = b1.GetNoDataValue()
        gdal.Warp(tmp, ds, xRes=res, yRes=res, resampleAlg='average', srcNodata=nodata,
                  dstNodata=-9999 if nodata is None else nodata, creationOptions=GTIFF_OPTIONS,
                  outputType=gdal.GDT_Float32)
        t = gdal.Open(tmp)
        grey = t.GetRasterBand(1).ReadAsArray().astype(np.float32)
        out_nd = t.GetRasterBand(1).GetNoDataValue()
        mask = (grey == out_nd) | ~np.isfinite(grey)
        grey[mask] = out_nd
        dtype = gdal.GDT_Float32
    drv = gdal.GetDriverByName('GTiff')
    o = drv.Create(dst, t.RasterXSize, t.RasterYSize, 1, dtype, GTIFF_OPTIONS)
    o.SetGeoTransform(t.GetGeoTransform())
    o.SetProjection(t.GetProjection())
    o.GetRasterBand(1).WriteArray(grey)
    o.GetRasterBand(1).SetNoDataValue(out_nd)
    o = t = ds = None
    os.remove(tmp)
    return float((~mask).sum()) * res * res / 1e4


def coregister(ref, target, out, grid_res=200, window_size=200, max_shift=60, max_iter=100, n_cores=None,
               min_points=10, resampling='cubic', points_csv=None):
    """
    Aligns `target` (RGB mosaic) on `ref` (grey reference from grey_reference()) with AROSICS COREG_LOCAL and
    writes the aligned mosaic `out` (same resolution as the target). Returns a dict of statistics.
    """
    import numpy as np
    from arosics import COREG_LOCAL
    from osgeo import gdal
    ds = gdal.Open(target)
    s_nd = ds.GetRasterBand(1).GetNoDataValue()
    s_nd = 255 if s_nd is None else s_nd
    s_band = 2 if ds.RasterCount >= 3 else 1               # green band of RGB mosaics: closest to luminance
    ds = None
    r = gdal.Open(ref)
    r_nd = r.GetRasterBand(1).GetNoDataValue()
    r = None
    tmp = out + '.tmp.tif'
    crl = COREG_LOCAL(ref, target, grid_res=grid_res, window_size=(window_size, window_size), max_shift=max_shift,
                      max_iter=max_iter, CPUs=n_cpus(n_cores), nodata=(r_nd, s_nd), r_b4match=1, s_b4match=s_band,
                      path_out=tmp, fmt_out='GTIFF', out_crea_options=GTIFF_OPTIONS, resamp_alg_calc='cubic',
                      resamp_alg_deshift=resampling, align_grids=False, q=False, progress=False, ignore_errors=True)
    tab = crl.CoRegPoints_table
    ok = tab[(tab['OUTLIER'] == False) & (tab['ABS_SHIFT'] != crl.outFillVal)] if 'OUTLIER' in tab else tab  # noqa: E712
    n = int(len(ok))
    if points_csv:
        tab.to_csv(points_csv, index=False)
    if n < min_points:
        raise RuntimeError(f'only {n} valid tie points (minimum {min_points}): mosaic too different from the reference '
                           f'(clouds, other season, little overlap?)')
    crl.correct_shifts()
    os.replace(tmp, out)
    return {'n_points': n, 'shift_x_m': round(float(np.median(ok['X_SHIFT_M'])), 3),
            'shift_y_m': round(float(np.median(ok['Y_SHIFT_M'])), 3),
            'shift_max_m': round(float(np.max(np.abs(ok['ABS_SHIFT']))) * 1, 3)}


def _read_journal(path):
    if not os.path.isfile(path):
        return {}
    with open(path, newline='', encoding='utf-8') as f:
        return {r['epoch']: r for r in csv.DictReader(f)}


def _write_journal(path, rows):
    with open(path + '.tmp', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (r['date'], r['epoch'])):
            w.writerow({k: r.get(k, '') for k in JOURNAL_FIELDS})
    os.replace(path + '.tmp', path)


def _fmt(s):
    return f'{s / 60:.0f} min' if s < 5400 else f'{s / 3600:.1f} h'


def align_series(mosaics_dir, out_dir, mode='previous', reference=None, ref_resolution=0.5,
                 min_ref_coverage=0.9, grid_res=200, window_size=200, max_shift=60, max_iter=100, n_cores=None,
                 min_points=10, resampling='cubic', overwrite=False, on_error='continue', epochs=None):
    """
    Aligns all the dated mosaics of mosaics_dir; results in out_dir (same file names), with align_journal.csv,
    the tie points of each mosaic (tie_points/) and the grey references (_references/). See the module doc.
    Returns the journal (list of dicts).
    """
    t_start = time.time()
    os.makedirs(out_dir, exist_ok=True)
    d_ref = os.path.join(out_dir, '_references')
    d_pts = os.path.join(out_dir, 'tie_points')
    os.makedirs(d_ref, exist_ok=True)
    os.makedirs(d_pts, exist_ok=True)
    f_journal = os.path.join(out_dir, 'align_journal.csv')
    jr = {} if overwrite else _read_journal(f_journal)
    mosaics = list_rasters(mosaics_dir)
    if epochs:
        mosaics = [m for m in mosaics if m[1] in set(epochs)]
    if not mosaics:
        raise ValueError(f'No mosaic with a date (YYYYMMDD) in its name in {mosaics_dir}')
    if mode not in ('previous', 'fixed', 'nearest'):
        raise ValueError("mode must be 'previous', 'fixed' or 'nearest'")
    if mode in ('fixed', 'nearest') and not reference:
        raise ValueError(f"mode '{mode}' needs reference (a raster for 'fixed', a folder of dated rasters for 'nearest')")

    def grey(path, tag):
        """grey 0.5 m version of a raster (cached); returns (path, hectares)."""
        g = os.path.join(d_ref, f'{tag}_{ref_resolution}m.tif')
        if not os.path.isfile(g) or os.path.getmtime(g) < os.path.getmtime(path):
            ha = grey_reference(path, g, ref_resolution)
        else:
            from osgeo import gdal
            import numpy as np
            ds = gdal.Open(g)
            a = ds.GetRasterBand(1).ReadAsArray()
            nd = ds.GetRasterBand(1).GetNoDataValue()
            ha = float((a != nd).sum()) * ref_resolution ** 2 / 1e4
        return g, ha

    refs = []
    if mode == 'nearest':
        refs = list_rasters(reference)
        if not refs:
            raise ValueError(f'No raster with a date (YYYYMMDD) in its name in {reference}')
        log(f'{len(refs)} dated references in {reference} ({refs[0][0]} to {refs[-1][0]})')

    # coverage of the mosaics (mode previous: a reference must cover min_ref_coverage of the largest one)
    cover = {}
    if mode == 'previous':
        log(f'Coverage of the {len(mosaics)} mosaics (grey {ref_resolution} m versions, cached in {d_ref})...')
        for d, e, p in mosaics:
            cover[e] = grey(p, 'mosaic_' + e)[1]
        cmax = max(cover.values())
        log(f'  largest mosaic: {cmax:.1f} ha; a reference must cover at least {min_ref_coverage * cmax:.1f} ha')

    todo = [m for m in mosaics if not (jr.get(m[1], {}).get('status') in ('done', 'anchor')
                                       and os.path.isfile(os.path.join(out_dir, os.path.basename(m[2]))))]
    log(f'{len(mosaics) - len(todo)}/{len(mosaics)} mosaics already aligned; {len(todo)} to align (mode {mode})')
    durations = []
    for i, (d, e, p) in enumerate(mosaics):
        out = os.path.join(out_dir, os.path.basename(p))
        if (d, e, p) not in todo:
            continue
        k = todo.index((d, e, p)) + 1
        t0 = time.time()
        rec = {'epoch': e, 'date': d, 'file': out, 'reference': '', 'status': 'failed', 'n_points': '',
               'shift_x_m': '', 'shift_y_m': '', 'shift_max_m': '', 'coverage_ha': cover.get(e, ''), 'message': ''}
        try:
            # choice of the reference
            if mode == 'fixed':
                ref_path, ref_name = reference, os.path.basename(reference)
            elif mode == 'nearest':
                rd, rn, rp = min(refs, key=lambda r: (abs(_days(r[0], d)), _days(r[0], d) < 0))   # tie: the one before
                ref_path, ref_name = rp, os.path.basename(rp)
            else:
                prev = [m for m in mosaics[:i] if jr.get(m[1], {}).get('status') in ('done', 'anchor')]
                good = [m for m in prev if cover.get(m[1], 0) >= min_ref_coverage * cmax]
                if not prev:
                    ref_path = reference
                    ref_name = os.path.basename(reference) if reference else ''
                else:
                    if not good:
                        log(f'WARNING: no previous aligned mosaic covers {min_ref_coverage:.0%} of the site: the last one is used')
                    rm = (good or prev)[-1]
                    ref_path = os.path.join(out_dir, os.path.basename(rm[2]))
                    ref_name = rm[1] + ' (aligned)'
            if mode == 'previous' and not ref_path:
                shutil.copy2(p, out)                       # anchor of the series: kept as it is
                rec.update(status='anchor', reference='', message='first mosaic: anchor of the series, not moved')
                log(f'[{k}/{len(todo)}] {e}: anchor of the series (first mosaic, copied as it is)')
            else:
                tag = 'ref_' + re.sub(r'[^A-Za-z0-9_-]', '_', os.path.splitext(os.path.basename(ref_path))[0])
                if ref_path.startswith(out_dir):
                    tag = 'aligned_' + os.path.splitext(os.path.basename(ref_path))[0]
                g, _ = grey(ref_path, tag)
                log(f'[{k}/{len(todo)}] {e} -> reference {ref_name} ...')
                st = coregister(g, p, out, grid_res=grid_res, window_size=window_size, max_shift=max_shift,
                                max_iter=max_iter, n_cores=n_cores, min_points=min_points, resampling=resampling,
                                points_csv=os.path.join(d_pts, e + '.csv'))
                rec.update(st, status='done', reference=ref_name)
                durations.append(time.time() - t0)
                left = (len(todo) - k) * sum(durations) / len(durations)
                log(f'[{k}/{len(todo)}] {e}: {st["n_points"]} tie points, median shift '
                    f'({st["shift_x_m"]}, {st["shift_y_m"]}) m, {time.time() - t0:.0f} s; remaining about {_fmt(left)}')
        except KeyboardInterrupt:
            raise
        except Exception as ex:  # noqa: BLE001 - recorded in the journal
            rec['message'] = f'{type(ex).__name__}: {ex}'.replace('\n', ' ')
            log(f'[{k}/{len(todo)}] {e}: FAILED: {rec["message"]}')
            for f in (out + '.tmp.tif',):
                if os.path.isfile(f):
                    os.remove(f)
            if on_error == 'stop':
                rec['seconds'] = round(time.time() - t0)
                jr[e] = rec
                _write_journal(f_journal, jr)
                raise
        rec['seconds'] = round(time.time() - t0)
        jr[e] = rec
        _write_journal(f_journal, jr)

    rows = [jr[m[1]] for m in mosaics if m[1] in jr]
    failed = [r for r in rows if r['status'] == 'failed']
    log(f'Alignment: {len(rows) - len(failed)}/{len(mosaics)} mosaics aligned'
        + (f', {len(failed)} failed: ' + ', '.join(r['epoch'] for r in failed) if failed else '')
        + f' ({_fmt(time.time() - t_start)} for this run). Journal: {f_journal}')
    return rows


def _days(a, b):
    """b - a in days (dates YYYYMMDD)."""
    import datetime
    da = datetime.date(int(a[:4]), int(a[4:6]), int(a[6:8]))
    db = datetime.date(int(b[:4]), int(b[4:6]), int(b[6:8]))
    return (db - da).days
