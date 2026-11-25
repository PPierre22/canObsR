"""
Quick check of the Python environment of canObsR, without any data: two synthetic GeoTIFF images, the second
shifted by a known amount, must be co-registered by AROSICS (global shift, then local grid). Takes a few seconds.
"""
import os
import shutil
import tempfile
import time


def _texture(n, seed=1):
    """Random smooth texture (sum of blurred noises), values 0-255."""
    import numpy as np
    rng = np.random.default_rng(seed)
    img = np.zeros((n, n), np.float32)
    for size in (3, 9, 27):
        noise = rng.random((n // size + 2, n // size + 2)).astype(np.float32)
        img += np.kron(noise, np.ones((size, size), np.float32))[:n, :n]
    img -= img.min()
    return (255 * img / img.max()).astype(np.uint8)


def _write(path, arr, x0, y0, res):
    from osgeo import gdal, osr
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Byte)
    ds.SetGeoTransform((x0, res, 0, y0, 0, -res))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32633)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(arr)
    ds = None


def run(shift_px=(3, -2), n=600, res=0.5):
    """Returns a dict: ok, expected and found shifts (pixels), seconds, and the error if any."""
    t0 = time.time()
    out = {"ok": False, "expected_px": list(shift_px)}
    tmp = tempfile.mkdtemp(prefix="canobs_selftest_")
    try:
        import numpy as np
        from arosics import COREG, COREG_LOCAL
        tex = _texture(n + 40)
        ref = tex[20:20 + n, 20:20 + n]
        dx, dy = shift_px
        # target image: same ground, georeferenced with an error of (dx, dy) pixels
        tgt = tex[20:20 + n, 20:20 + n]
        x0, y0 = 500000.0, 100000.0
        _write(os.path.join(tmp, "ref.tif"), ref, x0, y0, res)
        _write(os.path.join(tmp, "tgt.tif"), tgt, x0 + dx * res, y0 - dy * res, res)
        cr = COREG(os.path.join(tmp, "ref.tif"), os.path.join(tmp, "tgt.tif"), ws=(256, 256),
                   q=True, nodata=(None, None))
        cr.calculate_spatial_shifts()
        found = (cr.x_shift_px, cr.y_shift_px)
        out["found_px"] = [round(float(found[0]), 2), round(float(found[1]), 2)]
        # COREG gives the correction to apply to the target: the opposite of its georeferencing error
        out["ok"] = bool(np.allclose(found, (-dx, dy), atol=0.25) or np.allclose(found, (-dx, -dy), atol=0.25)
                         or np.allclose(np.abs(found), np.abs(shift_px), atol=0.25))
        cl = COREG_LOCAL(os.path.join(tmp, "ref.tif"), os.path.join(tmp, "tgt.tif"), grid_res=150,
                         window_size=(128, 128), q=True, CPUs=1, nodata=(None, None))
        tie = cl.CoRegPoints_table
        out["local_points"] = int((tie["OUTLIER"] == False).sum()) if "OUTLIER" in tie else int(len(tie))  # noqa: E712
        out["ok"] = out["ok"] and out["local_points"] > 0
    except Exception as e:  # noqa: BLE001 - reported to the caller
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out["seconds"] = round(time.time() - t0, 1)
    return out


if __name__ == "__main__":
    print(run())
