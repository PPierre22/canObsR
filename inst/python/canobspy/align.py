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
JOURNAL_FIELDS = ['epoch', 'date', 'chunk', 'file', 'reference', 'transform_from', 'status', 'n_points',
                  'shift_x_m', 'shift_y_m', 'shift_max_m', 'coverage_ha', 'seconds', 'message']


def log(*a):
    print(*a, flush=True)


try:
    from osgeo import gdal as _gdal
    _gdal.UseExceptions()
except Exception:  # noqa: BLE001 - GDAL is checked by canobs_setup()
    pass


N_CORES, MAX_MEMORY_GB = 'auto', 'auto'          # set by align_series()


def n_cpus(n=None, images=()):
    """
    AROSICS processes: n (integer), or 'auto': processors minus a few, limited by the memory (each process holds
    the band used for the matching of the target and the reference: about 2 x their size + 0.3 GB); at most 60 on
    Windows (limit of its process pool).
    """
    from .resources import auto_workers
    n = N_CORES if n in (None, 'auto') else n
    per = 0.3
    try:
        from osgeo import gdal
        for p in images:
            ds = gdal.Open(p)
            per += 2 * ds.RasterXSize * ds.RasterYSize * gdal.GetDataTypeSize(ds.GetRasterBand(1).DataType) / 8 / 1e9
            ds = None
    except Exception:  # noqa: BLE001 - size unknown: default estimate
        per = 2.0
    k, why = auto_workers(per, n, MAX_MEMORY_GB, cap=60 if sys.platform == 'win32' else None)
    log(f'    AROSICS processes: {why}')
    return k


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


def _geotransform(path):
    from osgeo import gdal
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    ds = None
    return gt


def coregister(ref, target, out, grid_res=200, window_size=200, max_shift=60, max_iter=100, n_cores=None,
               min_points=10, resampling='cubic', points_csv=None):
    """
    Aligns `target` (RGB mosaic) on `ref` (grey reference from grey_reference()) with AROSICS COREG_LOCAL, writes
    the aligned mosaic `out` (same resolution as the target) and returns (statistics, transformation). The
    transformation (tie points in map coordinates) can be applied to other mosaics with apply_transform().
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
                      max_iter=max_iter, CPUs=n_cpus(n_cores, (ref, target)), nodata=(r_nd, s_nd), r_b4match=1, s_b4match=s_band,
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
    info = crl.coreg_info
    gt = _geotransform(target)
    gcps = [[gt[0] + g.GCPPixel * gt[1] + g.GCPLine * gt[2], gt[3] + g.GCPPixel * gt[4] + g.GCPLine * gt[5],
             g.GCPX, g.GCPY] for g in info['GCPList']]
    transform = {'gcps_map': gcps, 'reference projection': info['reference projection'],
                 'reference geotransform': list(info['reference geotransform']),
                 'reference grid': [list(map(float, x)) for x in info['reference grid']],
                 'mean_shifts_px': {k: float(v) for k, v in info['mean_shifts_px'].items()},
                 'mean_shifts_map': {k: float(v) for k, v in info['mean_shifts_map'].items()},
                 'nodata': s_nd}
    stats = {'n_points': n, 'shift_x_m': round(float(np.median(ok['X_SHIFT_M'])), 3),
             'shift_y_m': round(float(np.median(ok['Y_SHIFT_M'])), 3),
             'shift_max_m': round(float(np.max(np.abs(ok['ABS_SHIFT']))), 3)}
    return stats, transform


def apply_transform(transform, target, out, n_cores=None, resampling='cubic'):
    """
    Applies the transformation measured on another mosaic of the same Time-SIFT chunk (same geometry) to `target`:
    the tie points, in map coordinates, are converted to pixel coordinates of this mosaic, then AROSICS warps it.
    """
    from arosics import DESHIFTER
    from osgeo import gdal
    gt = _geotransform(target)
    det = gt[1] * gt[5] - gt[2] * gt[4]
    gcps = []
    for xs, ys, xd, yd in transform['gcps_map']:
        dx, dy = xs - gt[0], ys - gt[3]
        px = (dx * gt[5] - dy * gt[2]) / det
        ln = (dy * gt[1] - dx * gt[4]) / det
        gcps.append(gdal.GCP(xd, yd, 0, px, ln))
    info = {'GCPList': gcps, 'reference projection': transform['reference projection'],
            'reference geotransform': transform['reference geotransform'],
            'reference grid': transform['reference grid'], 'mean_shifts_px': transform['mean_shifts_px'],
            'mean_shifts_map': transform['mean_shifts_map'], 'success': True}
    tmp = out + '.tmp.tif'
    DESHIFTER(target, info, path_out=tmp, fmt_out='GTIFF', out_crea_options=GTIFF_OPTIONS,
              nodata=transform.get('nodata', 255), align_grids=False, resamp_alg=resampling, CPUs=n_cpus(n_cores, (target,)),
              q=True).correct_shifts()
    os.replace(tmp, out)


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


def _chunks_of(mosaics, plan):
    """{epoch: chunk} from plan.csv of generate_mosaics() (a mosaic named <site_name><epoch>.tif)."""
    if not plan or not os.path.isfile(plan):
        return {}
    with open(plan, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    ep_chunk = {r['epoch']: r['chunk'] for r in rows}
    out = {}
    for d, e, p in mosaics:
        hits = [x for x in ep_chunk if e == x or e.endswith(x)]
        if hits:
            out[e] = ep_chunk[max(hits, key=len)]
    return out


def align_series(mosaics_dir, out_dir, mode='previous', reference=None, ref_resolution=0.5,
                 min_ref_coverage=0.9, grid_res=200, window_size=200, max_shift=60, max_iter=100, n_cores=None,
                 min_points=10, resampling='cubic', overwrite=False, on_error='continue', epochs=None,
                 plan=None, share_by_chunk=True, max_candidates=3, max_memory_gb='auto'):
    """
    Aligns all the dated mosaics of mosaics_dir; results in out_dir (same file names), with align_journal.csv,
    the tie points (tie_points/), the transformations (transforms/) and the grey references (_references/).

    Mosaics of the same Time-SIFT chunk (plan.csv of generate_mosaics) have the same geometry: with share_by_chunk,
    the transformation is measured once per chunk and reference, on its mosaic covering the largest area (or the
    next ones, up to max_candidates, if it fails), then applied to the other mosaics of the chunk.
    Returns the journal (list of dicts).
    """
    global N_CORES, MAX_MEMORY_GB
    N_CORES, MAX_MEMORY_GB = n_cores or 'auto', max_memory_gb
    t_start = time.time()
    os.makedirs(out_dir, exist_ok=True)
    d_ref, d_pts, d_tr = (os.path.join(out_dir, x) for x in ('_references', 'tie_points', 'transforms'))
    for d in (d_ref, d_pts, d_tr):
        os.makedirs(d, exist_ok=True)
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
    out_of = lambda m: os.path.join(out_dir, os.path.basename(m[2]))       # noqa: E731
    finished = lambda e: (jr.get(e, {}).get('status') in ('done', 'anchor')   # noqa: E731
                          and os.path.isfile(jr[e].get('file', '')))

    def grey(path, tag):
        """grey ref_resolution version of a raster (cached); returns (path, hectares)."""
        g = os.path.join(d_ref, f'{tag}_{ref_resolution}m.tif')
        if not os.path.isfile(g) or os.path.getmtime(g) < os.path.getmtime(path):
            ha = grey_reference(path, g, ref_resolution)
        else:
            from osgeo import gdal
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

    def ref_of(m):
        """reference raster of a mosaic in modes fixed and nearest."""
        if mode == 'fixed':
            return reference
        return min(refs, key=lambda r: (abs(_days(r[0], m[0])), _days(r[0], m[0]) < 0))[2]   # tie: the one before

    # units: mosaics sharing one transformation (same chunk and same reference), or single mosaics
    chunk = _chunks_of(mosaics, plan) if share_by_chunk else {}
    if chunk:
        log(f'Plan {plan}: {len(set(chunk.values()))} chunks. The transformation is measured once per chunk'
            + (' and reference' if mode != 'previous' else '') + ', then applied to the other mosaics of the chunk.')
    units = {}
    for m in mosaics:
        key = (chunk.get(m[1], 'mosaic_' + m[1]), ref_of(m) if mode != 'previous' else '')
        units.setdefault(key, []).append(m)

    # coverage (choice of the mosaic measured in each unit; mode previous: references covering enough of the site)
    cover = {}
    if mode == 'previous' or any(len(u) > 1 for u in units.values()):
        log(f'Coverage of the {len(mosaics)} mosaics (grey {ref_resolution} m versions, cached in {d_ref})...')
        for d, e, p in mosaics:
            cover[e] = grey(p, 'mosaic_' + e)[1]
    cmax = max(cover.values()) if cover else 0
    if mode == 'previous':
        log(f'  largest mosaic: {cmax:.1f} ha; a reference must cover at least {min_ref_coverage * cmax:.1f} ha')
    for u in units.values():                       # candidates to measure: largest coverage first, then date
        u.sort(key=lambda m: (-cover.get(m[1], 0), m[0], m[1]))
    order = sorted(units, key=lambda k: (units[k][0][0], units[k][0][1]))   # by date of the measured mosaic

    n_todo = sum(not finished(m[1]) for m in mosaics)
    log(f'{len(mosaics) - n_todo}/{len(mosaics)} mosaics already aligned; {n_todo} to align (mode {mode}, '
        f'{len(units)} transformation(s))')
    k, durations = 0, []
    measured = {}                                   # unit -> (epoch measured, aligned file) for mode previous

    for key in order:
        u = units[key]
        f_tr = os.path.join(d_tr, re.sub(r'[^A-Za-z0-9_.-]', '_', '_'.join(x for x in key if x) or key[0]) + '.json')
        done_rep = next((m for m in u if finished(m[1]) and jr[m[1]].get('transform_from') == m[1]), None)
        if done_rep:
            measured[key] = (done_rep[1], out_of(done_rep), jr[done_rep[1]]['status'])
        if all(finished(m[1]) for m in u):
            continue
        t0 = time.time()
        transform, rec_rep, ref_name = None, None, ''
        # 1. transformation of the unit: already measured, identity (anchor) or measured now
        if done_rep and (jr[done_rep[1]]['status'] == 'anchor' or os.path.isfile(f_tr)):
            if jr[done_rep[1]]['status'] != 'anchor':
                import json
                with open(f_tr, encoding='utf-8') as f:
                    transform = json.load(f)
            ref_name = jr[done_rep[1]].get('reference', '')
        else:
            if mode == 'previous':
                prev = [measured[o] for o in order[:order.index(key)] if o in measured]
                good = [x for x in prev if cover.get(x[0], 0) >= min_ref_coverage * cmax]
                if prev and not good:
                    log(f'WARNING: no aligned mosaic before covers {min_ref_coverage:.0%} of the site: the last one is used')
                ref_path = (good or prev)[-1][1] if prev else reference
                ref_name = ((good or prev)[-1][0] + ' (aligned)') if prev else (os.path.basename(reference) if reference else '')
            else:
                ref_path = key[1]
                ref_name = os.path.basename(ref_path)
            if mode == 'previous' and not ref_path:
                rep = u[0]
                for m in u:                              # anchor of the series: the whole unit is kept as it is
                    shutil.copy2(m[2], out_of(m))
                    jr[m[1]] = {'epoch': m[1], 'date': m[0], 'chunk': chunk.get(m[1], ''), 'file': out_of(m),
                                'reference': '', 'transform_from': rep[1], 'status': 'anchor',
                                'coverage_ha': cover.get(m[1], ''), 'seconds': 0,
                                'message': 'anchor of the series, not moved'}
                k += len(u)
                log(f'[{k}/{n_todo}] {rep[1]}' + (f' and the {len(u) - 1} other mosaics of {key[0]}' if len(u) > 1 else '')
                    + ': anchor of the series (copied as they are)')
                measured[key] = (rep[1], out_of(rep), 'anchor')
                _write_journal(f_journal, jr)
                continue
            if ref_path.startswith(out_dir):
                tag = 'aligned_' + os.path.splitext(os.path.basename(ref_path))[0]
            else:
                tag = 'ref_' + re.sub(r'[^A-Za-z0-9_-]', '_', os.path.splitext(os.path.basename(ref_path))[0])
            g, _ = grey(ref_path, tag)
            errors = []
            for m in u[:max(1, max_candidates)]:
                k_m = k + 1
                log(f'[{k_m}/{n_todo}] {m[1]} -> reference {ref_name}'
                    + (f' (transformation of {key[0]}: {len(u)} mosaics)' if len(u) > 1 else '') + ' ...')
                tm = time.time()
                try:
                    stats, transform = coregister(g, m[2], out_of(m), grid_res=grid_res, window_size=window_size,
                                                  max_shift=max_shift, max_iter=max_iter, n_cores=n_cores,
                                                  min_points=min_points, resampling=resampling,
                                                  points_csv=os.path.join(d_pts, m[1] + '.csv'))
                except KeyboardInterrupt:
                    raise
                except Exception as ex:  # noqa: BLE001 - next candidate, then recorded in the journal
                    errors.append(f'{m[1]}: {type(ex).__name__}: {ex}'.replace('\n', ' '))
                    log(f'    failed: {errors[-1]}' + ('; next mosaic of the chunk' if len(u) > len(errors) else ''))
                    if os.path.isfile(out_of(m) + '.tmp.tif'):
                        os.remove(out_of(m) + '.tmp.tif')
                    continue
                import json
                with open(f_tr, 'w', encoding='utf-8') as f:
                    json.dump(transform, f)
                rec_rep = {'epoch': m[1], 'date': m[0], 'chunk': chunk.get(m[1], ''), 'file': out_of(m),
                           'reference': ref_name, 'transform_from': m[1], 'status': 'done',
                           'coverage_ha': cover.get(m[1], ''), 'seconds': round(time.time() - tm), 'message': ''}
                rec_rep.update(stats)
                jr[m[1]] = rec_rep
                k += 1
                measured[key] = (m[1], out_of(m), 'done')
                log(f'[{k}/{n_todo}] {m[1]}: {stats["n_points"]} tie points, median shift '
                    f'({stats["shift_x_m"]}, {stats["shift_y_m"]}) m, {time.time() - tm:.0f} s')
                _write_journal(f_journal, jr)
                break
            if transform is None:
                for m in u:
                    if not finished(m[1]):
                        k += 1
                        jr[m[1]] = {'epoch': m[1], 'date': m[0], 'chunk': chunk.get(m[1], ''), 'file': out_of(m),
                                    'reference': ref_name, 'transform_from': '', 'status': 'failed',
                                    'coverage_ha': cover.get(m[1], ''), 'seconds': round(time.time() - t0),
                                    'message': ' | '.join(errors)}
                log(f'FAILED: no transformation for {key[0]} ({len(u)} mosaic(s)): ' + ' | '.join(errors))
                _write_journal(f_journal, jr)
                if on_error == 'stop':
                    raise RuntimeError(' | '.join(errors))
                continue
            done_rep = next(m for m in u if jr.get(m[1], {}).get('transform_from') == m[1])
        # 2. the other mosaics of the unit: same transformation
        for m in u:
            if finished(m[1]):
                continue
            tm = time.time()
            rec = {'epoch': m[1], 'date': m[0], 'chunk': chunk.get(m[1], ''), 'file': out_of(m), 'reference': ref_name,
                   'transform_from': done_rep[1], 'coverage_ha': cover.get(m[1], '')}
            try:
                if transform is None:                  # anchor unit
                    shutil.copy2(m[2], out_of(m))
                    rec.update(status='anchor', message='same chunk as the anchor, not moved')
                else:
                    apply_transform(transform, m[2], out_of(m), n_cores=n_cores, resampling=resampling)
                    rec.update(status='done', message=f'transformation of {done_rep[1]} (same chunk)')
            except KeyboardInterrupt:
                raise
            except Exception as ex:  # noqa: BLE001 - recorded in the journal
                rec.update(status='failed', message=f'{type(ex).__name__}: {ex}'.replace('\n', ' '))
                if on_error == 'stop':
                    jr[m[1]] = rec
                    _write_journal(f_journal, jr)
                    raise
            rec['seconds'] = round(time.time() - tm)
            jr[m[1]] = rec
            k += 1
            log(f'[{k}/{n_todo}] {m[1]}: {rec["status"]}, {rec["message"]} ({rec["seconds"]} s)')
            _write_journal(f_journal, jr)
        durations.append((time.time() - t0) / len(u))
        left = (n_todo - k) * sum(durations) / len(durations)
        log(f'    remaining about {_fmt(left)}')

    rows = [{f: jr[m[1]].get(f, '') for f in JOURNAL_FIELDS} for m in mosaics if m[1] in jr]
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
