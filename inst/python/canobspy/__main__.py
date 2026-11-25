"""
Command line of canobspy, used by the R package canobsR (it runs Python in a separate process, never inside R).

    python -m canobspy info                          versions of Python, canobspy, numpy, GDAL, AROSICS (JSON)
    python -m canobspy selftest                      AROSICS finds a known shift between two synthetic images
    python -m canobspy call MODULE FUNCTION ARGS.json RESULT.json
                                                     calls canobspy.MODULE.FUNCTION(**ARGS) and writes its result

Why a separate process: R (terra, sf) and Python (GDAL of conda-forge) load different versions of GDAL and PROJ;
in the same process they can conflict (crash, or "proj.db" errors). A separate process also keeps R alive if
Python fails, and lets R stop it cleanly (Esc / Ctrl+C).
"""
import importlib
import json
import platform
import sys
import traceback


def _metashape_valid(Metashape):
    """True if a Metashape licence is usable (node-locked activation, or floating licence via agisoft_LICENSE)."""
    try:
        return bool(Metashape.License().valid)
    except Exception:  # noqa: BLE001 - older API
        return bool(getattr(Metashape.app, "activated", False))


def _metashape_activate(key):
    """Activates Metashape on this computer with a licence key (needs internet access to agisoft.com)."""
    import Metashape
    if _metashape_valid(Metashape):
        return {"ok": True, "message": "already activated"}
    try:
        Metashape.License().activate(key)
    except Exception as e:  # noqa: BLE001 - sent back to R
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}
    return {"ok": _metashape_valid(Metashape), "message": "activated" if _metashape_valid(Metashape) else "not valid after activation"}


def _versions():
    import canobspy
    out = {"python": platform.python_version(), "canobspy": canobspy.__version__,
           "executable": sys.executable.replace("\\", "/")}
    for name, mod, attr in (("numpy", "numpy", "__version__"), ("gdal", "osgeo.gdal", "__version__"),
                            ("arosics", "arosics", "__version__"), ("metashape", "Metashape", None)):
        try:
            m = importlib.import_module(mod)
            out[name] = m.app.version if name == "metashape" else getattr(m, attr)
            if name == "metashape":
                out["metashape_activated"] = _metashape_valid(m)
        except Exception as e:  # noqa: BLE001 - reported, not raised
            out[name] = None
            out[name + "_error"] = f"{type(e).__name__}: {e}"
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:                                   # GDAL errors as Python exceptions (and no FutureWarning of GDAL 4)
        from osgeo import gdal
        gdal.UseExceptions()
    except Exception:  # noqa: BLE001 - GDAL is checked by canobs_setup()
        pass
    cmd = argv[0] if argv else "info"
    if cmd == "info":
        print(json.dumps(_versions()))
        return 0
    if cmd == "metashape-activate" and len(argv) == 2:
        res = _metashape_activate(argv[1])
        print(json.dumps(res))
        return 0 if res["ok"] else 1
    if cmd == "selftest":
        from canobspy import selftest
        res = selftest.run()
        print(json.dumps(res))
        return 0 if res["ok"] else 1
    if cmd == "call" and len(argv) == 5:
        module, function, args_file, result_file = argv[1:]
        with open(args_file, encoding="utf-8") as f:
            kwargs = json.load(f)
        try:
            fun = getattr(importlib.import_module(f"canobspy.{module}"), function)
            res = fun(**kwargs)
            out = {"ok": True, "result": res}
        except BaseException as e:  # noqa: BLE001 - the error is sent back to R
            if not isinstance(e, (ValueError, FileNotFoundError)):    # errors of use: the message is enough
                traceback.print_exc()
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(out, f, default=str)
        return 0 if out["ok"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
