# =====================================================================================================
# Benchmark: the same data processed from scratch by generate_Mosa() with engine = "metashape" and with
# engine = "odm", in new folders, timed; then comparison of the results (compare_Mosa_engines.R).
# =====================================================================================================
# To get comparable times: run on the same machine, with nothing else running, the two engines one after
# the other (this script). Both use the same parameters (the defaults of generate_Mosa()).
# The temporary files and the ODM projects are written on D: (tempdir_custom, out_dir_project).
# =====================================================================================================

lib_dev <- "D:/canObsR/Rlib_dev"
library(canObsR, lib.loc = lib_dev)
library(reticulate)
use_condaenv("C:/ProgramData/miniconda3/envs/arosics", required = TRUE)   # with Metashape API and arosics

my_path   <- "D:/canObsR/data/canObsR_data/canObsR_data"
path_in   <- file.path(my_path, "1_my_drone_data")
bench_dir <- file.path(my_path, "benchmark")                 # everything produced by the benchmark goes here
odm_path  <- "C:/ODM/run.bat"

# common parameters (same as the test vignette)
common <- list(path_in = path_in, resol_ref = 0.05, site_name = "Bouamir_", crs = "EPSG::32633")

dir_ref <- file.path(bench_dir, "ortho_metashape")
dir_odm <- file.path(bench_dir, "ortho_odm")
dir_out <- file.path(bench_dir, "compare")
dir.create(dir_out, recursive = TRUE, showWarnings = FALSE)

# 0. check of the image folders (information only)
chk <- check_flights(path_in)
write.csv(chk$folders, file.path(dir_out, "check_flights.csv"), row.names = FALSE)

timings <- data.frame(engine = character(0), minutes = numeric(0), start = character(0), end = character(0))
save_time <- function(engine, t, start) {
   timings <<- rbind(timings, data.frame(engine = engine, minutes = round(t[["elapsed"]] / 60, 1),
                                         start = format(start), end = format(Sys.time())))
   write.csv(timings, file.path(dir_out, "timings_engines.csv"), row.names = FALSE)
   message(engine, ": ", round(t[["elapsed"]] / 60, 1), " min")
}

# 1. Metashape
start <- Sys.time()
t_ms <- system.time(
   do.call(generate_Mosa, c(common, list(out_dir_ortho = dir_ref,
                                         out_dir_project = file.path(bench_dir, "project_metashape"),
                                         engine = "metashape")))
)
save_time("Metashape", t_ms, start)

# 2. ODM (from scratch: overwrite = TRUE)
start <- Sys.time()
t_odm <- system.time(
   do.call(generate_Mosa, c(common, list(out_dir_ortho = dir_odm,
                                         out_dir_project = file.path(bench_dir, "project_odm"),
                                         engine = "odm", odm_path = odm_path, overwrite = TRUE,
                                         tempdir_custom = file.path(bench_dir, "tmp"))))
)
save_time("ODM", t_odm, start)

# 3. comparison (uses dir_ref, dir_odm, dir_out defined above)
py_file <- "D:/canObsR/compare_engines.py"
tmp_dir <- file.path(bench_dir, "tmp_compare")
source("D:/canObsR/compare_Mosa_engines.R")
