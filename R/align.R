.align_options <- list(ref_resolution = 0.5, min_ref_coverage = 0.9, grid_res = 200L, window_size = 200L,
                       max_shift = 60L, max_iter = 100L, n_cores = NULL, min_points = 10L, resampling = "cubic",
                       overwrite = FALSE, on_error = "continue", epochs = NULL)

#' Co-register a series of orthomosaics
#'
#' @description
#' Aligns each orthomosaic of `mosaics_dir` on a reference with AROSICS (local co-registration: shifts measured on a
#' grid of tie points, then applied to the whole mosaic), and writes the aligned mosaics in `out_dir`, with the same
#' file names. Mosaics are dated by the `YYYYMMDD` of their file name (as made by [generate_mosaics()]).
#'
#' The reference is first aggregated to `ref_resolution` (0.5 m) and converted to grey (mean of the RGB bands; white
#' background = no data): the result is practically the same as at 5 cm, many times faster. Three modes:
#' * `"previous"` (default): each mosaic is aligned on the **last mosaic already aligned** before it in time that
#'   covers at least `min_ref_coverage` (90 %) of the largest mosaic (partial flights are not used as references).
#'   The first mosaic is the anchor of the series and is not moved, unless `reference` is given: it is then aligned
#'   on it;
#' * `"fixed"`: all the mosaics on **one reference raster** (`reference`): an RGB mosaic, or a single-band raster
#'   (e.g. a lidar canopy model or intensity image);
#' * `"nearest"`: `reference` is a **folder of dated rasters** (`YYYYMMDD` in the file names): each mosaic is aligned
#'   on the closest one in time, before or after (the one before if two are as close).
#'
#' Like [generate_mosaics()], it can be stopped and run again: `out_dir/align_journal.csv` records each mosaic as soon
#' as it is aligned (reference, number of tie points, median shift), and the failed mosaics are retried. The tie
#' points of each mosaic are saved in `out_dir/tie_points/` and the grey references in `out_dir/_references/`.
#'
#' @param mosaics_dir character. Folder of the orthomosaics (e.g. `out_dir/orthomosaics` of [generate_mosaics()]).
#' @param out_dir character. Folder of the aligned mosaics.
#' @param mode character. `"previous"`, `"fixed"` or `"nearest"`.
#' @param reference character. `"fixed"`: reference raster; `"nearest"`: folder of dated reference rasters;
#'   `"previous"`: optional raster on which the first mosaic is aligned.
#' @param ... Advanced options (an unknown name is an error): `ref_resolution` (0.5 m), `min_ref_coverage` (0.9),
#'   AROSICS parameters `grid_res` (200: distance between tie points, in pixels of the mosaic), `window_size` (200:
#'   matching window, pixels), `max_shift` (60 pixels), `max_iter` (100), `n_cores` (processes; default
#'   min(processors, 16)), `min_points` (10: fewer valid tie points = failure), `resampling` ("cubic"); `overwrite`
#'   (FALSE), `on_error` ("continue" or "stop"), `epochs` (names of the mosaics to align, default all).
#' @return Invisibly, the journal: a data.frame with one row per mosaic (`epoch`, `date`, `file`, `reference`,
#'   `status`: "anchor", "done" or "failed", `n_points`, `shift_x_m`, `shift_y_m`, `shift_max_m`, `coverage_ha`,
#'   `seconds`, `message`).
#' @export
#' @examples
#' \dontrun{
#' # each date on the previous one
#' align_mosaics("D:/bouamir/2_mosaics/orthomosaics", "D:/bouamir/3_aligned")
#' # all the dates on a lidar canopy model
#' align_mosaics("D:/bouamir/2_mosaics/orthomosaics", "D:/bouamir/3_aligned_lidar",
#'               mode = "fixed", reference = "D:/bouamir/lidar/chm_2022.tif")
#' # each date on the closest of a set of reference mosaics
#' align_mosaics("D:/bouamir/2_mosaics/orthomosaics", "D:/bouamir/3_aligned_refs",
#'               mode = "nearest", reference = "D:/bouamir/references")
#' }
align_mosaics <- function(mosaics_dir, out_dir, mode = c("previous", "fixed", "nearest"), reference = NULL, ...) {
   mode <- match.arg(mode)
   dots <- list(...)
   bad <- setdiff(names(dots), names(.align_options))
   if (length(bad)) stop("Unknown option(s): ", paste(bad, collapse = ", "), ". Options: ",
                         paste(names(.align_options), collapse = ", "), " (see ?align_mosaics).", call. = FALSE)
   opt <- utils::modifyList(.align_options, dots, keep.null = TRUE)
   opt$on_error <- match.arg(opt$on_error, c("continue", "stop"))
   if (!dir.exists(mosaics_dir)) stop("Folder not found: ", mosaics_dir, call. = FALSE)
   if (mode == "fixed" && (is.null(reference) || !file.exists(reference) || dir.exists(reference)))
      stop("mode = \"fixed\": give the reference raster (reference = \"...tif\").", call. = FALSE)
   if (mode == "nearest" && (is.null(reference) || !dir.exists(reference)))
      stop("mode = \"nearest\": give the folder of dated reference rasters (reference = \"...\").", call. = FALSE)
   if (mode == "previous" && !is.null(reference) && !file.exists(reference))
      stop("Reference not found: ", reference, call. = FALSE)
   dir.create(file.path(out_dir, "logs"), recursive = TRUE, showWarnings = FALSE)
   np <- function(p) if (is.null(p)) NULL else normalizePath(p, winslash = "/")
   rule <- strrep("=", 100)
   message(rule, "\nalign_mosaics(): ", np(mosaics_dir), "\n  -> ", np(out_dir), "\n  mode ", mode,
           if (!is.null(reference)) paste0(", reference ", reference), "\n", rule)
   old <- options(canobsR.call_log = file.path(out_dir, "logs", "align.log")); on.exit(options(old), add = TRUE)
   args <- c(list("align", "align_series", mosaics_dir = np(mosaics_dir), out_dir = np(out_dir), mode = mode,
                  reference = np(reference)), opt)
   if (!is.null(opt$epochs)) args$epochs <- as.list(opt$epochs)
   rows <- do.call(.canobs_call, args)
   jr <- do.call(rbind, lapply(rows, function(r) as.data.frame(lapply(r, function(v) if (is.null(v)) NA else v),
                                                            stringsAsFactors = FALSE)))
   for (k in c("n_points", "shift_x_m", "shift_y_m", "shift_max_m", "coverage_ha", "seconds"))
      if (k %in% names(jr)) jr[[k]] <- suppressWarnings(as.numeric(jr[[k]]))
   invisible(jr)
}
