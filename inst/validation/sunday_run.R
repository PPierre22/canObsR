# =====================================================================================================
# Long unattended run: GPU test of ODM, then benchmark3, colour calibration test, mesh density test
# =====================================================================================================
# To run in a NEW R session (Session > Restart R), with all the files in D:/canObsR:
#   canObsR_0.0.0.9012_odm.tar.gz, sunday_run.R, odm_gpu_test.R, benchmark_engines.R, compare_Mosa_engines.R,
#   compare_engines.py, colour_calibration_test.R, odm_mesh_size_test.R
#   source("D:/canObsR/sunday_run.R")
# Each step is independent: if one fails, the error is written in the log and the next one starts.
# Log: <my_path>/sunday_run_log.txt (start, end and duration of each step, errors).
#
# Expected duration: several hours, mostly the ODM alignment with 100 000 keypoints per image (to measure).
#   1. GPU test of ODM ............................ 15-60 min
#   2. benchmark3: Metashape then ODM, comparison . Metashape ~45 min, ODM several hours, comparison ~30 min
#   3. colour calibration "date" (both engines) ... ~1-1.5 h (ODM: texture only; Metashape: full run)
#   4. ODM mesh density 1 M and 4 M vertices ...... ~1-2 h (meshing + texture + ortho), comparisons ~1 h
# =====================================================================================================

scripts  <- "D:/canObsR"
lib_dev  <- "D:/canObsR/Rlib_dev"
my_path  <- "D:/canObsR/data/canObsR_data/canObsR_data"
run_log  <- file.path(my_path, "sunday_run_log.txt")

log_line <- function(...) { m <- paste0(format(Sys.time(), "%Y-%m-%d %H:%M:%S"), "  ", ...); message(m)
                            cat(m, "\n", file = run_log, append = TRUE) }

# ---- 0. canObsR 0.0.0.9012 -----------------------------------------------------------------------
v <- tryCatch(as.character(packageVersion("canObsR", lib.loc = lib_dev)), error = function(e) "none")
if (v != "0.0.0.9012") {
   if ("canObsR" %in% loadedNamespaces()) stop("canObsR ", v, " is loaded: restart R (Session > Restart R) and run this script again")
   install.packages(file.path(scripts, "canObsR_0.0.0.9012_odm.tar.gz"), repos = NULL, type = "source", lib = lib_dev)
}
log_line("canObsR ", as.character(packageVersion("canObsR", lib.loc = lib_dev)), " — start of the run")

step <- function(name, file) {
   t0 <- Sys.time()
   log_line("START ", name)
   ok <- tryCatch({ source(file.path(scripts, file), echo = FALSE); TRUE },
                  error = function(e) { log_line("ERROR in ", name, ": ", conditionMessage(e)); FALSE })
   try(system("taskkill /IM nvidia-smi.exe /F", ignore.stdout = TRUE, ignore.stderr = TRUE), silent = TRUE)
   log_line(if (ok) "END   " else "FAILED ", name, " (", round(as.numeric(difftime(Sys.time(), t0, units = "mins")), 1), " min)")
   invisible(ok)
}

# 1. does OpenMVS use the GPU? (if the fix works, the next ODM runs use it)
step("1. GPU test of ODM", "odm_gpu_test.R")
if (exists("gpu_fix_applied")) log_line("   GPU fix applied: ", gpu_fix_applied)

# 2. both engines from scratch with canObsR 0.0.0.9012, then AROSICS comparison
step("2. benchmark3 (Metashape, ODM, comparison)", "benchmark_engines.R")

# 3. colour calibration date by date, for both engines
step("3. colour calibration test", "colour_calibration_test.R")

# 4. ODM: density of the mesh used for the orthomosaics
step("4. ODM mesh size test", "odm_mesh_size_test.R")

log_line("end of the run. Results: ", file.path(my_path, "benchmark2", "gpu_test"), " and ", file.path(my_path, "benchmark3"))
