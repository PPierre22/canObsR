"""
canobspy: processing chain of the canobs drone observatories (https://www.canobs.net).

Time-SIFT orthomosaics of long series of drone flights (OpenDroneMap or Agisoft Metashape) and their
co-registration with AROSICS. Used by the R package canobsR, or on its own (command line, scripts).

Submodules are imported on demand, so that importing canobspy never requires Metashape, ODM or AROSICS:
    from canobspy import timesift_odm     # Time-SIFT with OpenDroneMap
    from canobspy import timesift_metashape  # Time-SIFT with Agisoft Metashape (licence needed)
    from canobspy import compare           # comparison of two series of orthomosaics (AROSICS)
"""
import importlib

__version__ = "0.9.0.9000"  # same version as the R package canobsR

_SUBMODULES = ("flights", "timesift_odm", "timesift_metashape", "compare", "selftest", "align", "aggregate")


def __getattr__(name):
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
