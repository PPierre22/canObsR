<!-- README.md is generated from README.Rmd. Please edit that file -->

canobsR
=======

[![Licence MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

canobsR is the processing chain of the [canobs](https://www.canobs.net) drone observatories: multi-temporal
orthomosaics of long series of drone flights with the Time-SIFT method (OpenDroneMap by default, or Agisoft
Metashape), and their co-registration with AROSICS.

The processing is done by **canobspy**, a Python package shipped with canobsR and run in a separate conda
environment. The phenology tools (crown images, labels, shiny applications) of the versions 0.x will be
published in a separate package.

## Installation

```r
install.packages("remotes")
remotes::install_github("umr-amap/canobsR")
canobsR::canobs_setup()     # Python environment (conda-forge), checks, ODM detection: 5-15 min, once
```

No administrator rights, Rtools or existing Python are needed. See the
[installation guide](https://umr-amap.github.io/canobsR/articles/Installation-guide.html) for ODM, Metashape,
the GPU and troubleshooting; `canobsR::canobs_sitrep()` prints a diagnosis to join to any bug report.

## Main functions

| Function | |
|---|---|
| `canobs_setup()`, `canobs_sitrep()` | installation and diagnosis of the Python environment, ODM, Metashape |
| `check_flights()` | checks the image folders (dates in the EXIF, GPS, duplicates) before processing |
| `generate_Mosa()` | Time-SIFT orthomosaics (and DSMs), `engine = "odm"` (default) or `"metashape"` |
| `check_odm_epochs()` | checks that the ODM projects of each date contain only images of this date |

## Citation

To cite 'canobsR', please use `citation('canobsR')`.
