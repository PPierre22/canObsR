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

## In short

```r
library(canobsR)
check_flights("D:/site/1_flights")                                  # one subfolder per flight: RGB_20250101, ...
generate_mosaics("D:/site/1_flights", "D:/site/2_mosaics")          # one orthomosaic per date (Time-SIFT, chunks)
align_mosaics("D:/site/2_mosaics/orthomosaics", "D:/site/3_aligned") # co-registration (AROSICS)
aggregate_mosaics("D:/site/3_aligned", "D:/site/4_mosaics_50cm", indices = c("GCC", "VARI"))  # 50 cm + indices
```

Both processing functions are made for long series (hundreds of dates, hundreds of hectares at 5 cm) on one
computer: they can be stopped and run again (they resume where they stopped), they keep a journal, report their
progress and errors, and process new flights as they come.

## Tutorials

* [Installation](https://umr-amap.github.io/canobsR/articles/Installation-guide.html)
* [Orthomosaics of a long series of flights](https://umr-amap.github.io/canobsR/articles/Generate-orthomosaics.html)
* [Co-registration of a series of orthomosaics](https://umr-amap.github.io/canobsR/articles/Align-orthomosaics.html)
* [Lighter mosaics and spectral indices](https://umr-amap.github.io/canobsR/articles/Aggregate-mosaics.html)
* [Lighter mosaics and spectral indices](https://umr-amap.github.io/canobsR/articles/Aggregate-mosaics.html)

## Citation

To cite 'canobsR', please use `citation('canobsR')`.
