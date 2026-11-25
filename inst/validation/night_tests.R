# =====================================================================================================
# Overnight tests: final settings of generate_Mosa(engine = "odm"), compared with Metashape
# =====================================================================================================
# Questions:
#   1. how many keypoints per image for ODM? 100 000 (= Metashape) took 16 h of alignment in benchmark3;
#      tested here: 20 000 and 10 000, with all the other settings of canobsR 0.0.0.9014 and the GPU;
#   2. does a denser mesh (--mesh-size) bring the ODM orthomosaics closer to Metashape? (ODM default 200 000
#      vertices; tested 1 000 000 and 4 000 000, from the meshing step only).
# Reference: the Metashape series of benchmark3 (canobsR 0.0.0.9012, same Metashape settings as before).
# Each ODM series is compared with it (AROSICS, sections A for ODM, B ODM vs Metashape, C colours).
#
# To run in a NEW R session, with canobsR_0.0.0.9014_odm.tar.gz, night_tests.R, compare_Mosa_engines.R,
# compare_engines.py and colour_calibration_test.R in D:/canobsR:
#   source("D:/canobsR/night_tests.R")
# Every step is independent (an error is logged, the next step starts). A step is skipped when its expected
# duration goes beyond `stop_by` (verzy is needed at 8:00). Everything is in <my_path>/benchmark4:
#   night_log.txt (progress), night_summary.csv / night_summary.txt (one row per series), compare_*/summary.txt.
# A step interrupted (crash, stop) is resumed by running the script again: finished ODM dates are skipped.
# =====================================================================================================

scripts  <- "D:/canobsR"
lib_dev  <- "D:/canobsR/Rlib_dev"
my_path  <- "D:/canobsR/data/canobsR_data/canobsR_data"
path_in  <- file.path(my_path, "1_my_drone_data")
b3       <- file.path(my_path, "benchmark3")
b4       <- file.path(my_path, "benchmark4")
odm_path <- "C:/ODM/run.bat"
stop_by  <- as.POSIXct(paste(Sys.Date() + 1, "07:45:00"))          # tomorrow 07:45
# steps to run (set before source() to run only some of them, e.g. night_steps <- "mesh")
if (!exists("night_steps")) night_steps <- c("colour", "k20000", "k10000", "mesh")
if (!exists("mesh_on"))     mesh_on     <- "k10000"                 # project used for the mesh test
if (!exists("mesh_sizes"))  mesh_sizes  <- c(1000000, 4000000)
dir.create(b4, showWarnings = FALSE, recursive = TRUE)
night_log <- file.path(b4, "night_log.txt")

log_line <- function(...) { m <- paste0(format(Sys.time(), "%Y-%m-%d %H:%M:%S"), "  ", ...); message(m)
                            cat(m, "\n", file = night_log, append = TRUE) }

# ---- 0. package, GPU, disk -------------------------------------------------------------------------
v <- tryCatch(as.character(packageVersion("canobsR", lib.loc = lib_dev)), error = function(e) "none")
if (v != "0.0.0.9014") {
   if ("canobsR" %in% loadedNamespaces()) stop("canobsR ", v, " is loaded: restart R and run this script again")
   install.packages(file.path(scripts, "canobsR_0.0.0.9014_odm.tar.gz"), repos = NULL, type = "source", lib = lib_dev)
}
library(canobsR, lib.loc = lib_dev); library(reticulate); library(terra)
use_condaenv("C:/ProgramData/miniconda3/envs/arosics", required = TRUE)
log_line("canobsR ", as.character(packageVersion("canobsR", lib.loc = lib_dev)), " - start, stop_by ", format(stop_by))
dummy <- "C:/ODM/SuperBuild/install/bin/nvcuda.dll"
log_line("GPU: dummy nvcuda.dll next to OpenMVS: ", if (file.exists(dummy) && file.size(dummy) < 1e6)
         "PRESENT (generate_Mosa() will rename it)" else "absent (OpenMVS uses the GPU)")
free_gb <- function() {
   x <- suppressWarnings(system('wmic logicaldisk where "DeviceID=\'D:\'" get FreeSpace /value', intern = TRUE))
   as.numeric(sub(".*=", "", grep("FreeSpace=", x, value = TRUE))) / 1e9
}
log_line("free space on D: ", round(free_gb()), " GB")

# ---- helpers ---------------------------------------------------------------------------------------
smi <- Sys.which("nvidia-smi")
gpu_start <- function(f) if (nzchar(smi)) system2(smi, c("--query-gpu=timestamp,utilization.gpu,memory.used",
                          "--format=csv,noheader,nounits", "-l", "10", "-f", shQuote(normalizePath(f, mustWork = FALSE))), wait = FALSE)
gpu_stop  <- function() system("taskkill /IM nvidia-smi.exe /F", ignore.stdout = TRUE, ignore.stderr = TRUE)
gpu_mean  <- function(f) tryCatch({ g <- read.csv(f, header = FALSE); round(mean(as.numeric(g[[2]]), na.rm = TRUE), 1) },
                                  error = function(e) NA)

night_common <- list(path_in = path_in, resol_ref = 0.05, site_name = "Bouamir", crs = "EPSG::32633", engine = "odm",
               odm_path = odm_path, tempdir_custom = file.path(b4, "tmp"))

# one ODM series (from scratch or resumed), then its comparison with Metashape (benchmark3)
step <- function(name, expected_h, args, project) {
   if (Sys.time() + expected_h * 3600 > stop_by) {
      log_line("SKIPPED ", name, ": about ", expected_h, " h needed, not enough time before ", format(stop_by, "%H:%M"))
      return(invisible(NULL))
   }
   if (free_gb() < 80) { log_line("SKIPPED ", name, ": less than 80 GB free on D:"); return(invisible(NULL)) }
   out <- file.path(b4, paste0("ortho_", name))
   log_line("START ", name, " (expected ~", expected_h, " h)")
   gpu_file <- file.path(b4, paste0("gpu_", name, ".csv")); unlink(gpu_file); gpu_start(gpu_file)
   t0 <- Sys.time()
   ok <- tryCatch({ do.call(generate_Mosa, c(night_common, args, list(out_dir_ortho = out, out_dir_project = project))); TRUE },
                  error = function(e) { log_line("ERROR in ", name, ": ", conditionMessage(e)); FALSE })
   gpu_stop()
   minutes <- round(as.numeric(difftime(Sys.time(), t0, units = "mins")), 1)
   log_line(if (ok) "END   " else "FAILED ", name, ": ", minutes, " min, mean GPU use ", gpu_mean(gpu_file), " %")
   if (!ok) return(invisible(NULL))
   write.csv(data.frame(engine = "ODM", minutes = minutes), file.path(b4, paste0("time_", name, ".csv")), row.names = FALSE)
   # comparison with Metashape (between dates: ODM only, Metashape is known from benchmark3)
   t1 <- Sys.time()
   ok2 <- tryCatch({
      dir_ref <<- file.path(b3, "ortho_metashape"); dir_odm <<- out; dir_out <<- file.path(b4, paste0("compare_", name))
      tmp_dir <<- file.path(b4, "tmp_compare"); py_file <<- file.path(scripts, "compare_engines.py")
      CPUs <<- 16; engines_A <<- "ODM"
      dir.create(dir_out, showWarnings = FALSE)
      file.copy(file.path(b4, paste0("time_", name, ".csv")), file.path(dir_out, "timings_engines.csv"), overwrite = TRUE)
      source(file.path(scripts, "compare_Mosa_engines.R")); TRUE },
      error = function(e) { log_line("ERROR in the comparison of ", name, ": ", conditionMessage(e)); FALSE })
   log_line(if (ok2) "END   " else "FAILED ", "comparison of ", name, ": ",
            round(as.numeric(difftime(Sys.time(), t1, units = "mins")), 1), " min")
   update_summary()
   invisible(ok)
}

# one row per series: times (ODM stages), AROSICS results
update_summary <- function() {
   rows <- list()
   series <- c(b3_k100k_mesh200k = file.path(b3, "compare"),
               setNames(list.files(b4, "^compare_", full.names = TRUE),
                        sub("^compare_", "b4_", list.files(b4, "^compare_"))))
   for (s in names(series)) {
      d <- series[[s]]
      A <- tryCatch(read.csv(file.path(d, "A_between_dates.csv")), error = function(e) NULL)
      B <- tryCatch(read.csv(file.path(d, "B_odm_vs_metashape.csv")), error = function(e) NULL)
      Tm <- tryCatch(read.csv(file.path(d, "timings_engines.csv")), error = function(e) NULL)
      if (!is.null(A)) A <- A[A$engine == "ODM", ]
      rows[[s]] <- data.frame(series = s,
         odm_minutes = if (!is.null(Tm)) Tm$minutes[Tm$engine == "ODM"][1] else NA,
         A_odm_median_shift_cm = if (!is.null(A) && nrow(A)) round(100 * mean(A$median_abs_m), 2) else NA,
         A_odm_valid_pct = if (!is.null(A) && nrow(A)) round(mean(A$pct_valid), 1) else NA,
         B_offset_x_cm = if (!is.null(B)) round(100 * mean(B$median_dx_m), 1) else NA,
         B_offset_y_cm = if (!is.null(B)) round(100 * mean(B$median_dy_m), 1) else NA,
         B_local_dev_cm = if (!is.null(B)) round(100 * mean(B$median_dev_from_median_m), 1) else NA,
         B_valid_pct = if (!is.null(B)) round(mean(B$pct_valid), 1) else NA)
   }
   tab <- do.call(rbind, rows)
   # ODM stage times of each project
   for (p in list.files(b4, "^project_odm_", full.names = TRUE)) {
      t <- tryCatch(read.csv(file.path(p, "timings.csv")), error = function(e) NULL)
      if (is.null(t)) next
      log_line("   times of ", basename(p), " (min): SfM ", round(sum(t$seconds[t$project == "timesift_block"]) / 60, 1),
               ", openmvs ", round(sum(t$seconds[t$stage == "openmvs"]) / 60, 1),
               ", meshing ", round(sum(t$seconds[t$stage == "odm_meshing"]) / 60, 1))
   }
   write.csv(tab, file.path(b4, "night_summary.csv"), row.names = FALSE)
   writeLines(capture.output(print(tab, row.names = FALSE)), file.path(b4, "night_summary.txt"))
   print(tab, row.names = FALSE)
}

# ---- 1. colour statistics of benchmark3 (series already computed) -----------------------------------
if ("colour" %in% night_steps) tryCatch({ run_odm_date <- FALSE; run_metashape_date <- FALSE
           source(file.path(scripts, "colour_calibration_test.R")); log_line("END   colour statistics (benchmark3)") },
         error = function(e) log_line("ERROR in the colour statistics: ", conditionMessage(e)))

# ---- 2. ODM 20 000 keypoints (full run: alignment ~1.5-2 h, dense on GPU, comparison) ---------------
if ("k20000" %in% night_steps) step("k20000", 4.5, list(keypoint_limit = 20000L), file.path(b4, "project_odm_k20000"))

# ---- 3. ODM 10 000 keypoints (full run: alignment ~45 min) -----------------------------------------
if ("k10000" %in% night_steps) step("k10000", 3.5, list(keypoint_limit = 10000L), file.path(b4, "project_odm_k10000"))

# ---- 4. mesh density, on an existing project (meshing, texture and orthomosaic only) ----------------
kp <- as.integer(sub("^k", "", mesh_on))
if ("mesh" %in% night_steps) for (m in mesh_sizes) {
   if (dir.exists(file.path(b4, paste0("project_odm_", mesh_on), "20220427")))
      step(paste0(mesh_on, "_mesh", format(m, scientific = FALSE)), 2.5,
           list(keypoint_limit = kp, odm_args = c("--mesh-size", format(m, scientific = FALSE))),
           file.path(b4, paste0("project_odm_", mesh_on)))
   else log_line("SKIPPED mesh test: no project ", mesh_on)
}

log_line("end of the night. Summary: ", file.path(b4, "night_summary.txt"))
update_summary()
