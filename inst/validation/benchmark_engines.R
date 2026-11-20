# =====================================================================================================
# Benchmark: the same data processed from scratch by generate_Mosa() with engine = "metashape" and with
# engine = "odm", in new folders, timed; then comparison of the results (compare_Mosa_engines.R).
# =====================================================================================================
# - run on the same machine, with nothing else running, the two engines one after the other (this script);
# - both engines get the same parameters and the same subfolders (those with a date in their name);
# - GPU usage is logged every 5 s with nvidia-smi during each run (gpu_usage_<engine>.csv);
# - the console output is also written to console_<engine>.txt (times of each step for Metashape);
# - everything is written in bench_dir; temporary files and ODM projects stay on D:.
# =====================================================================================================

lib_dev <- "D:/canObsR/Rlib_dev"
# install.packages("D:/canObsR/canObsR_0.0.0.9010_odm.tar.gz", repos = NULL, type = "source", lib = lib_dev)  # once, R restarted
library(canObsR, lib.loc = lib_dev)
library(reticulate)
use_condaenv("C:/ProgramData/miniconda3/envs/arosics", required = TRUE)   # with Metashape API and arosics

my_path   <- "D:/canObsR/data/canObsR_data/canObsR_data"
path_in   <- file.path(my_path, "1_my_drone_data")
bench_dir <- file.path(my_path, "benchmark2")                # everything produced by the benchmark goes here
odm_path  <- "C:/ODM/run.bat"
run_metashape <- TRUE
run_odm       <- TRUE

# common parameters (same as the test vignette); site_name without "_": both engines add it
common <- list(path_in = path_in, resol_ref = 0.05, site_name = "Bouamir", crs = "EPSG::32633")

dir_ref <- file.path(bench_dir, "ortho_metashape")
dir_odm <- file.path(bench_dir, "ortho_odm")
dir_out <- file.path(bench_dir, "compare")
dir.create(dir_out, recursive = TRUE, showWarnings = FALSE)

# ---- helpers ------------------------------------------------------------------------------------
gpu_start <- function(file) {                      # nvidia-smi in the background, one line every 5 s
   exe <- Sys.which("nvidia-smi")
   if (exe == "") { message("nvidia-smi not found: no GPU log"); return(invisible(FALSE)) }
   system2(exe, c("--query-gpu=timestamp,utilization.gpu,memory.used", "--format=csv,noheader,nounits",
                  "-l", "5", "-f", shQuote(file)), wait = FALSE)
   invisible(TRUE)
}
gpu_stop <- function() invisible(system("taskkill /IM nvidia-smi.exe /F", ignore.stdout = TRUE, ignore.stderr = TRUE))

tee_start <- function(file) py_run_string(sprintf("
import sys
class _CanobsTee:
    def __init__(self, path, stream):
        self.f = open(path, 'a', encoding='utf-8', errors='replace'); self.s = stream
    def write(self, x):
        self.s.write(x); self.f.write(x); self.f.flush()
    def flush(self):
        self.s.flush(); self.f.flush()
_canobs_stdout = sys.stdout
sys.stdout = _CanobsTee(r'%s', sys.stdout)
", normalizePath(file, winslash = "/", mustWork = FALSE)))
tee_stop <- function() py_run_string("
import sys
if '_canobs_stdout' in globals():
    sys.stdout.f.close(); sys.stdout = _canobs_stdout
")

timings <- if (file.exists(file.path(dir_out, "timings_engines.csv"))) read.csv(file.path(dir_out, "timings_engines.csv")) else
   data.frame(engine = character(0), minutes = numeric(0), start = character(0), end = character(0), status = character(0))
run_engine <- function(engine, args) {
   gpu_file <- file.path(dir_out, paste0("gpu_usage_", engine, ".csv")); unlink(gpu_file)
   con_file <- file.path(dir_out, paste0("console_", engine, ".txt")); unlink(con_file)
   gpu_start(gpu_file); tee_start(con_file)
   start <- Sys.time()
   status <- tryCatch({ do.call(generate_Mosa, args); "ok" }, error = function(e) { message(conditionMessage(e)); paste("error:", conditionMessage(e)) })
   end <- Sys.time()
   tee_stop(); gpu_stop()
   timings <<- rbind(timings[timings$engine != engine, , drop = FALSE],
                     data.frame(engine = engine, minutes = round(as.numeric(difftime(end, start, units = "mins")), 1),
                                start = format(start), end = format(end), status = status))
   write.csv(timings, file.path(dir_out, "timings_engines.csv"), row.names = FALSE)
   message(engine, ": ", round(as.numeric(difftime(end, start, units = "mins")), 1), " min (", status, ")")
}

# 0. check of the image folders (information only)
chk <- check_flights(path_in)
write.csv(chk$folders, file.path(dir_out, "check_flights.csv"), row.names = FALSE)

# 1. Metashape
if (run_metashape) run_engine("Metashape", c(common, list(out_dir_ortho = dir_ref,
                                                          out_dir_project = file.path(bench_dir, "project_metashape"),
                                                          engine = "metashape")))
# 2. ODM (from scratch: overwrite = TRUE)
if (run_odm) run_engine("ODM", c(common, list(out_dir_ortho = dir_odm, out_dir_project = file.path(bench_dir, "project_odm"),
                                              engine = "odm", odm_path = odm_path, overwrite = TRUE,
                                              tempdir_custom = file.path(bench_dir, "tmp"))))

# ---- 3. times per step and GPU usage ------------------------------------------------------------
read_gpu <- function(f) {
   if (!file.exists(f) || file.size(f) == 0) return(NULL)
   x <- read.csv(f, header = FALSE, col.names = c("time", "util", "mem"), strip.white = TRUE)
   x$time <- as.POSIXct(x$time, format = "%Y/%m/%d %H:%M:%OS")
   x[!is.na(x$time), ]
}
steps <- NULL
# Metashape: "Time ... : <s> seconds" lines printed by the canObsR Metashape pipeline
f_con <- file.path(dir_out, "console_Metashape.txt")
if (file.exists(f_con)) {
   l <- grep("^Time .*: *[0-9.]+ seconds", readLines(f_con, warn = FALSE), value = TRUE)
   if (length(l)) steps <- rbind(steps, data.frame(engine = "Metashape", step = sub("^Time (to |spent (for )?)?(.*?) *: .*$", "\\3", l),
                                                   seconds = as.numeric(sub("^.*: *([0-9.]+) seconds.*$", "\\1", l)),
                                                   gpu_util_mean = NA))
}
# ODM: timings.csv of the ODM project (start and end of each ODM stage)
f_odm <- file.path(bench_dir, "project_odm", "timings.csv")
g_odm <- read_gpu(file.path(dir_out, "gpu_usage_ODM.csv"))
if (file.exists(f_odm)) {
   t <- read.csv(f_odm)
   if (!is.null(g_odm) && all(c("start", "end") %in% names(t))) {
      t$gpu_util_mean <- mapply(function(s, e) {
         v <- g_odm$util[g_odm$time >= as.POSIXct(s) & g_odm$time <= as.POSIXct(e)]
         if (length(v)) mean(v) else NA }, t$start, t$end)
   } else t$gpu_util_mean <- NA
   steps <- rbind(steps, data.frame(engine = "ODM", step = paste(t$project, t$stage), seconds = t$seconds,
                                    gpu_util_mean = round(t$gpu_util_mean, 1)))
}
if (!is.null(steps)) {
   write.csv(steps, file.path(dir_out, "steps_engines.csv"), row.names = FALSE)
   print(steps[steps$seconds >= 30, ], row.names = FALSE)
}
gpu_summary <- do.call(rbind, lapply(c("Metashape", "ODM"), function(e) {
   g <- read_gpu(file.path(dir_out, paste0("gpu_usage_", e, ".csv")))
   if (is.null(g)) return(NULL)
   data.frame(engine = e, samples = nrow(g), gpu_util_mean = round(mean(g$util), 1),
              pct_time_gpu_busy = round(100 * mean(g$util > 20), 1), gpu_mem_max_MB = max(g$mem))
}))
if (!is.null(gpu_summary)) {
   write.csv(gpu_summary, file.path(dir_out, "gpu_summary.csv"), row.names = FALSE)
   print(gpu_summary, row.names = FALSE)
}
print(timings, row.names = FALSE)

# ---- 4. comparison of the mosaics (uses dir_ref, dir_odm, dir_out defined above) ----------------
py_file <- "D:/canObsR/compare_engines.py"
tmp_dir <- file.path(bench_dir, "tmp_compare")
source("D:/canObsR/compare_Mosa_engines.R")
