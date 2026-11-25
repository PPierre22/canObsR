"""
Command line of canobspy, used by the R package canObsR (it runs Python in a separate process, never inside R).

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
                out["metashape_activated"] = bool(getattr(m.app, "activated", False))
        except Exception as e:  # noqa: BLE001 - reported, not raised
            out[name] = None
            out[name + "_error"] = f"{type(e).__name__}: {e}"
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "info"
    if cmd == "info":
        print(json.dumps(_versions()))
        return 0
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
            traceback.print_exc()
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(out, f, default=str)
        return 0 if out["ok"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
