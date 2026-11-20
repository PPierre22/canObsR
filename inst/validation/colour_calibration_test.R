# =====================================================================================================
# Colour calibration "chunk" (all dates together) vs "date" (each date alone), for both engines
# =====================================================================================================
# To run AFTER benchmark_engines.R, which produced the "chunk" mosaics (default) in bench_dir:
#   - ODM "date": reuses the ODM project of the benchmark: only the texture and the orthomosaics are computed
#     again (same alignment, point cloud and mesh: only the colour calibration differs);
#   - Metashape "date": complete run (Metashape does not keep the point clouds between runs).
# Then colour statistics of the 4 series, per date: mean R, G, B and GCC = G / (R + G + B) on the area common
# to all mosaics, and their changes between dates. The question: does "date" keep the phenological changes
# between dates better than "chunk" (which harmonises the colours between dates)?
# =====================================================================================================

lib_dev <- "D:/canObsR/Rlib_dev"
library(canObsR, lib.loc = lib_dev)
library(reticulate)
library(terra)
use_condaenv("C:/ProgramData/miniconda3/envs/arosics", required = TRUE)

my_path   <- "D:/canObsR/data/canObsR_data/canObsR_data"
path_in   <- file.path(my_path, "1_my_drone_data")
bench_dir <- file.path(my_path, "benchmark3")
odm_path  <- "C:/ODM/run.bat"
run_odm_date       <- TRUE
run_metashape_date <- TRUE
common <- list(path_in = path_in, resol_ref = 0.05, site_name = "Bouamir", crs = "EPSG::32633", calibrate_col = TRUE)

series <- c(Metashape_chunk = file.path(bench_dir, "ortho_metashape"),
            Metashape_date  = file.path(bench_dir, "ortho_metashape_date"),
            ODM_chunk       = file.path(bench_dir, "ortho_odm"),
            ODM_date        = file.path(bench_dir, "ortho_odm_date"))
dir_out <- file.path(bench_dir, "compare_colour")
dir.create(dir_out, recursive = TRUE, showWarnings = FALSE)
terraOptions(tempdir = file.path(bench_dir, "tmp_colour"))
dir.create(file.path(bench_dir, "tmp_colour"), showWarnings = FALSE)

times <- list()
if (run_odm_date) {
   t <- system.time(do.call(generate_Mosa, c(common, list(
      out_dir_ortho = series[["ODM_date"]], out_dir_project = file.path(bench_dir, "project_odm"),
      calibrate_col_by = "date", engine = "odm", odm_path = odm_path, tempdir_custom = file.path(bench_dir, "tmp")))))
   times$ODM_date <- t[["elapsed"]] / 60
}
if (run_metashape_date) {
   t <- system.time(do.call(generate_Mosa, c(common, list(
      out_dir_ortho = series[["Metashape_date"]], out_dir_project = file.path(bench_dir, "project_metashape_date"),
      calibrate_col_by = "date", engine = "metashape"))))
   times$Metashape_date <- t[["elapsed"]] / 60
}
if (length(times)) print(data.frame(run = names(times), minutes = round(unlist(times), 1)), row.names = FALSE)

# ---- colour statistics on the common area --------------------------------------------------------
files <- lapply(series, function(d) {
   f <- list.files(d, pattern = "\\.tif$", full.names = TRUE)
   setNames(f, regmatches(basename(f), regexpr("\\d{8}", basename(f))))
})
dates <- sort(Reduce(intersect, lapply(files, names)))
if (length(dates) == 0) stop("No date common to the 4 series")
common_ext <- Reduce(terra::intersect, lapply(unlist(files), function(f) ext(rast(f))))
grid <- rast(common_ext, resolution = 0.5, crs = crs(rast(files[[1]][[1]])))     # statistics on 0.5 m pixels

stats <- do.call(rbind, lapply(names(series), function(s) do.call(rbind, lapply(dates, function(d) {
   r <- rast(files[[s]][[d]])[[1:3]]
   if (!same.crs(r, grid)) r <- project(r, crs(grid))
   r <- mask(r, (r[[1]] >= 254 & r[[2]] >= 254 & r[[3]] >= 254) | (r[[1]] + r[[2]] + r[[3]] == 0), maskvalues = 1)
   v <- values(resample(r, grid, method = "average"))
   v <- v[stats::complete.cases(v), , drop = FALSE]
   gcc <- v[, 2] / rowSums(v)
   data.frame(series = s, date = d, R = mean(v[, 1]), G = mean(v[, 2]), B = mean(v[, 3]),
              GCC = mean(gcc), GCC_sd_in_mosaic = sd(gcc), n_pixels = nrow(v))
}))))
# change of each date relative to the first one
stats <- do.call(rbind, lapply(split(stats, stats$series), function(x) {
   x <- x[order(x$date), ]
   x$dGCC_vs_first <- x$GCC - x$GCC[1]
   x$dG_vs_first_pct <- 100 * (x$G / x$G[1] - 1)
   x
}))
write.csv(stats, file.path(dir_out, "colour_stats.csv"), row.names = FALSE)
print(stats, digits = 4, row.names = FALSE)

png(file.path(dir_out, "GCC_by_date.png"), width = 900, height = 600)
cols <- c(Metashape_chunk = "#1b9e77", Metashape_date = "#1b9e77", ODM_chunk = "#d95f02", ODM_date = "#d95f02")
ltys <- c(Metashape_chunk = 1, Metashape_date = 2, ODM_chunk = 1, ODM_date = 2)
x <- as.Date(dates, "%Y%m%d")
plot(range(x), range(stats$GCC), type = "n", xlab = "date", ylab = "mean GCC (common area)")
for (s in names(series)) {
   y <- stats$GCC[stats$series == s][order(stats$date[stats$series == s])]
   lines(x, y, col = cols[s], lty = ltys[s], lwd = 2); points(x, y, col = cols[s], pch = 19)
}
legend("topleft", legend = names(series), col = cols, lty = ltys, lwd = 2, bty = "n")
dev.off()
tmpFiles(current = TRUE, orphan = FALSE, old = FALSE, remove = TRUE)
message("Results in ", dir_out)
