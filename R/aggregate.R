#' RGB spectral indices available in aggregate_mosaics()
#'
#' @return A data.frame: `name`, `formula` (R, G, B: digital numbers of the bands; r, g, b: chromatic coordinates,
#'   e.g. g = G / (R + G + B)) and `reference`.
#' @export
#' @examples
#' \dontrun{
#' rgb_indices()
#' }
rgb_indices <- function() {
   x <- .canobs_call("aggregate", "list_indices", .echo = FALSE)
   data.frame(name = vapply(x, function(i) i$name, ""), formula = vapply(x, function(i) i$formula, ""),
              reference = vapply(x, function(i) i$reference, ""), stringsAsFactors = FALSE)
}

.aggregate_options <- list(common_extent = TRUE, min_coverage = 0.5, background = c(255, 0), overwrite = FALSE,
                           epochs = NULL, suffix = NULL)

#' Lighter mosaics: coarser resolution and spectral indices
#'
#' @description
#' Makes, for each orthomosaic of `mosaics_dir` (typically the aligned mosaics of [align_mosaics()]), a lighter
#' version at `resolution` (0.5 m by default), easy to handle in QGIS or R, with the bands R, G, B and, optionally,
#' RGB spectral indices (see [rgb_indices()] for the list and formulas).
#'
#' * Each value is the **mean of the pixels** of the mosaic in the cell: an index is computed on each pixel (e.g. at
#'   5 cm), then averaged, like the colour bands (mean of the index, not index of the mean colour).
#' * All the outputs are on **one grid**, aligned on multiples of the resolution and covering all the mosaics (option
#'   `common_extent`): the dates overlay exactly and can be stacked directly (`terra::rast(files)`).
#' * The background of the mosaics (pixels white or black in the 3 bands) is no data; cells covered by less than half
#'   of mosaic pixels are no data (NaN).
#' * Output: `out_dir/<mosaic name>_50cm.tif` (`_1m` for 1 m, etc.), float32, one band per variable, named (`R`,
#'   `G`, `B`, `GCC`, ...). Mosaics already done are skipped: the function can be run again after new dates.
#'
#' @param mosaics_dir character. Folder of the mosaics.
#' @param out_dir character. Output folder.
#' @param resolution numeric. Pixel size of the outputs, in metres.
#' @param indices character. Indices to add after R, G and B, e.g. `c("GCC", "VARI")`; `"all"` for all of them;
#'   NULL (default) for R, G, B only.
#' @param ... Advanced options (an unknown name is an error): `common_extent` (TRUE), `min_coverage` (0.5: minimum
#'   fraction of a cell covered by mosaic pixels), `background` (c(255, 0): values of the background in the 3 bands),
#'   `overwrite` (FALSE), `epochs` (names of the mosaics to process), `suffix` (default "50cm" for 0.5 m).
#' @return Invisibly, a data.frame with one row per mosaic: `file`, `output`, `status` ("done", "exists",
#'   "failed"), `hectares`, `seconds`.
#' @export
#' @examples
#' \dontrun{
#' aggregate_mosaics("D:/bouamir/3_aligned", "D:/bouamir/4_mosaics_50cm", indices = c("GCC", "RCC", "BCC", "VARI"))
#' s <- terra::rast(list.files("D:/bouamir/4_mosaics_50cm", full.names = TRUE))   # all dates, all bands
#' }
aggregate_mosaics <- function(mosaics_dir, out_dir, resolution = 0.5, indices = NULL, ...) {
   dots <- list(...)
   bad <- setdiff(names(dots), names(.aggregate_options))
   if (length(bad)) stop("Unknown option(s): ", paste(bad, collapse = ", "), ". Options: ",
                         paste(names(.aggregate_options), collapse = ", "), " (see ?aggregate_mosaics).", call. = FALSE)
   opt <- utils::modifyList(.aggregate_options, dots, keep.null = TRUE)
   if (!dir.exists(mosaics_dir)) stop("Folder not found: ", mosaics_dir, call. = FALSE)
   if (identical(indices, "all")) indices <- rgb_indices()$name
   dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
   rows <- .canobs_call("aggregate", "aggregate_series",
                        mosaics_dir = normalizePath(mosaics_dir, winslash = "/"),
                        out_dir = normalizePath(out_dir, winslash = "/"), resolution = resolution,
                        indices = as.list(indices), common_extent = opt$common_extent, min_coverage = opt$min_coverage,
                        background = as.list(opt$background), overwrite = opt$overwrite,
                        epochs = if (is.null(opt$epochs)) NULL else as.list(opt$epochs), suffix = opt$suffix)
   out <- do.call(rbind, lapply(rows, function(r) data.frame(file = r$file, output = r$output, status = r$status,
                                                             hectares = if (is.null(r$hectares)) NA_real_ else r$hectares,
                                                             seconds = r$seconds, stringsAsFactors = FALSE)))
   invisible(out)
}
