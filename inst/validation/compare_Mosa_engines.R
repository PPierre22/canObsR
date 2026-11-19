# =====================================================================================================
# Comparison of two series of orthomosaics produced on the same data: Metashape (reference) vs ODM
# =====================================================================================================
# Measures (AROSICS local co-registration, same settings as canObsR align_Mosa()):
#   A. co-registration BETWEEN DATES inside each engine: every date vs the first date. This is the quality
#      of the Time-SIFT (the reason why it is used): the smaller the residual shifts, the better. Both
#      engines are compared on this criterion.
#   B. ODM vs Metashape for each date: global offset between the two engines (georeferencing from the
#      drone GPS, so a global offset of a few dm to m is expected) and local distortion around it
#      ('median_dev_from_median_m'). The offset should be about the same for all dates.
#   C. colours: mean and standard deviation of R, G, B per mosaic (white background excluded), and the
#      variation between dates.
#   D. processing times, if benchmark_engines.R was used (file timings_engines.csv).
# Sign of the shifts (AROSICS): dx, dy are the CORRECTION to apply to the target (2nd image of the pair) to
# superimpose it on the reference, i.e. the opposite of its displacement. Magnitudes (|shift|) are signless.
# Precision checked on synthetic mosaics with known shifts: ~1 cm (shifts below one pixel are slightly
# underestimated: 1.4 cm measured for 2 cm at 5 cm pixels).
# Outputs in dir_out: one csv per table, one vector map (png) per pair, summary.txt.
# =====================================================================================================

library(terra)
library(reticulate)

# ---- settings (a value already defined, e.g. by benchmark_engines.R, is kept) ----------------------
default <- function(name, value) if (!exists(name, envir = globalenv())) assign(name, value, envir = globalenv())
default("my_path",   "D:/canObsR/data/canObsR_data/canObsR_data")
default("dir_ref",   file.path(my_path, "2_orthomosaics"))        # Metashape
default("dir_odm",   file.path(my_path, "2_orthomosaics_odm2"))   # ODM
default("dir_out",   file.path(my_path, "compare_engines"))
default("py_file",   "D:/canObsR/compare_engines.py")             # python file delivered with this script
default("conda_env", "C:/ProgramData/miniconda3/envs/arosics")    # environment with arosics
default("tmp_dir",   "D:/canObsR/tmp/compare")                    # temporary files of terra (not on C:)
default("CPUs",      NULL)                                        # NULL = all
# --------------------------------------------------------------------------------------------------

if (!py_available()) use_condaenv(conda_env, required = TRUE)
dir.create(dir_out, showWarnings = FALSE, recursive = TRUE)
dir.create(tmp_dir, showWarnings = FALSE, recursive = TRUE)
terraOptions(tempdir = tmp_dir)
cmp <- new.env()
source_python(py_file, envir = cmp)

s_ref <- unlist(cmp$series(dir_ref)); s_odm <- unlist(cmp$series(dir_odm))
dates <- sort(intersect(names(s_ref), names(s_odm)))
if (length(dates) == 0) stop("No common date between ", dir_ref, " and ", dir_odm)
message("Dates: ", paste(dates, collapse = ", "))
if (length(setdiff(names(s_ref), dates))) message("Only in Metashape: ", paste(setdiff(names(s_ref), dates), collapse = ", "))
if (length(setdiff(names(s_odm), dates))) message("Only in ODM: ", paste(setdiff(names(s_odm), dates), collapse = ", "))

run_pair <- function(ref, tgt, name) {
   message("AROSICS: ", name)
   r <- cmp$coreg_pair(ref, tgt, file.path(dir_out, name), CPUs = CPUs)
   as.data.frame(lapply(r, function(v) if (is.null(v)) NA else v), stringsAsFactors = FALSE)
}

# ---- A. between dates, inside each engine --------------------------------------------------------
tabA <- NULL
if (length(dates) > 1) {
   for (eng in c("Metashape", "ODM")) {
      s <- if (eng == "Metashape") s_ref else s_odm
      for (d in dates[-1]) {
         tabA <- rbind(tabA, cbind(engine = eng, date_ref = dates[1], date = d,
                                   run_pair(s[[dates[1]]], s[[d]], paste0("A_", eng, "_", d, "_vs_", dates[1]))))
      }
   }
   write.csv(tabA, file.path(dir_out, "A_between_dates.csv"), row.names = FALSE)
}

# ---- B. ODM vs Metashape, date by date -----------------------------------------------------------
tabB <- do.call(rbind, lapply(dates, function(d)
   cbind(date = d, run_pair(s_ref[[d]], s_odm[[d]], paste0("B_ODM_vs_Metashape_", d)))))
write.csv(tabB, file.path(dir_out, "B_odm_vs_metashape.csv"), row.names = FALSE)

# ---- C. colours ----------------------------------------------------------------------------------
col_stats <- function(f, eng, d) {
   r <- rast(f)[[1:3]]
   fact <- max(1, round(0.5 / res(r)[1]))                       # statistics on 0.5 m pixels
   if (fact > 1) r <- aggregate(r, fact, fun = "mean", na.rm = TRUE)
   v <- values(r)
   v <- v[stats::complete.cases(v) & !(v[, 1] >= 254 & v[, 2] >= 254 & v[, 3] >= 254) & rowSums(v) > 0, , drop = FALSE]
   gcc <- v[, 2] / rowSums(v)                                      # green chromatic coordinate
   data.frame(engine = eng, date = d, file = basename(f), res_m = res(rast(f))[1],
              size_MB = round(file.size(f) / 1e6), area_ha = round(nrow(v) * (res(r)[1])^2 / 1e4, 2),
              R_mean = mean(v[, 1]), G_mean = mean(v[, 2]), B_mean = mean(v[, 3]),
              R_sd = sd(v[, 1]), G_sd = sd(v[, 2]), B_sd = sd(v[, 3]), GCC_mean = mean(gcc))
}
tabC <- rbind(do.call(rbind, lapply(dates, function(d) col_stats(s_ref[[d]], "Metashape", d))),
              do.call(rbind, lapply(dates, function(d) col_stats(s_odm[[d]], "ODM", d))))
write.csv(tabC, file.path(dir_out, "C_colours.csv"), row.names = FALSE)

# ---- D. processing times -------------------------------------------------------------------------
f_time <- file.path(dir_out, "timings_engines.csv")
tabD <- if (file.exists(f_time)) read.csv(f_time) else NULL

# ---- summary -------------------------------------------------------------------------------------
num <- function(x, k = 3) formatC(x, format = "f", digits = k)
out <- c("Comparison Metashape (reference) vs ODM", paste("Dates:", paste(dates, collapse = ", ")), "")
if (!is.null(tabA)) {
   out <- c(out, "A. Co-registration between dates (each date vs the first one; smaller = better Time-SIFT)")
   for (eng in c("Metashape", "ODM")) {
      x <- tabA[tabA$engine == eng, ]
      out <- c(out, sprintf("   %-9s median |shift| per date: %s m | p90: %s m | valid points: %s %%", eng,
                            paste(num(x$median_abs_m), collapse = ", "), paste(num(x$p90_abs_m), collapse = ", "),
                            paste(x$pct_valid, collapse = ", ")))
   }
   out <- c(out, "")
}
out <- c(out, "B. ODM vs Metashape (global offset should be about the same for all dates)",
         sprintf("   %s: offset (%s, %s) m, |offset| %s m, local deviation %s m, valid points %s %%",
                 tabB$date, num(tabB$median_dx_m), num(tabB$median_dy_m), num(tabB$median_abs_m),
                 num(tabB$median_dev_from_median_m), tabB$pct_valid),
         sprintf("   spread of the offset between dates: %s m (x), %s m (y)",
                 num(diff(range(tabB$median_dx_m))), num(diff(range(tabB$median_dy_m)))), "",
         "C. Colours (mean R, G, B; GCC = G / (R + G + B))",
         sprintf("   %-9s %s: %s / %s / %s  GCC %s", tabC$engine, tabC$date, num(tabC$R_mean, 1), num(tabC$G_mean, 1),
                 num(tabC$B_mean, 1), num(tabC$GCC_mean, 4)))
for (eng in c("Metashape", "ODM")) {
   x <- tabC[tabC$engine == eng, ]
   out <- c(out, sprintf("   %-9s variation between dates (sd of the means): R %s, G %s, B %s, GCC %s", eng,
                         num(sd(x$R_mean), 2), num(sd(x$G_mean), 2), num(sd(x$B_mean), 2), num(sd(x$GCC_mean), 4)))
}
if (!is.null(tabD)) {
   out <- c(out, "", "D. Processing times", sprintf("   %-9s %s min", tabD$engine, num(tabD$minutes, 1)))
}
writeLines(out, file.path(dir_out, "summary.txt"))
cat(out, sep = "\n")

tmpFiles(current = TRUE, orphan = FALSE, old = FALSE, remove = TRUE)
unlink(tmp_dir, recursive = TRUE)
