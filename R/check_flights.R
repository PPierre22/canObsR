#' Check the drone image folders before processing
#'
#' @description
#' Reads the EXIF of all the images of `path_in` (one subfolder per date, e.g. `RGB_20250101`) and reports, for each
#' subfolder, the days on which the images were taken, the first and last shot, the images without GPS tags and the
#' image files found twice. Nothing is changed and no error is raised: it is a check to run before [generate_Mosa()].
#'
#' The subfolder defines the date: in [generate_Mosa()], all the images of a subfolder make one mosaic, even if some of
#' them were taken on another day (e.g. a flight interrupted by rain and finished the next day). `check_flights()` only
#' points out these cases, so that they can be checked.
#'
#' @param path_in character. Folder with one subfolder per date.
#' @param folder_names character. Subfolders to check (all by default).
#' @param check_duplicates logical. Looks for identical image files (reads the beginning of each file).
#' @param verbose logical. Prints the folders to check.
#'
#' @return A list of two data.frames:
#' * `folders`: one row per subfolder: `folder_date` (date in the folder name), `epoch` (date used by
#'   [generate_Mosa()], NA if the folder is ignored), `n_images`, `exif_days` (days found in the EXIF, with the number of
#'   images), `n_days`, `first_shot`, `last_shot`, `n_duplicates`, `n_no_gps`, `status` ("ok", "check" or "ignored")
#'   and `notes`;
#' * `images`: one row per image: `folder`, `file`, `folder_date`, `exif_datetime`, `exif_day`, `gps`, `duplicate_of`.
#' @export
#'
#' @examples
#' \dontrun{
#' chk <- check_flights("D:/my_data/rawData")
#' chk$folders[chk$folders$status != "ok", ]
#' }
check_flights <- function(path_in, folder_names = NULL, check_duplicates = TRUE, verbose = TRUE) {
   res <- .canobs_call("timesift_odm", "flights_report", path_in = normalizePath(path_in, winslash = "/"),
                            folder_names = if (is.null(folder_names)) NULL else as.list(folder_names),
                            check_duplicates = check_duplicates)
   to_df <- function(x, cols) {
      if (length(x) == 0) return(as.data.frame(structure(replicate(length(cols), logical(0), simplify = FALSE), names = cols)))
      do.call(rbind, lapply(x, function(r) as.data.frame(lapply(r[cols], function(v) if (is.null(v)) NA else v),
                                                           stringsAsFactors = FALSE)))
   }
   images <- to_df(res[[1]], c("folder", "file", "folder_date", "exif_datetime", "exif_day", "gps", "duplicate_of"))
   folders <- to_df(res[[2]], c("folder", "folder_date", "epoch", "n_images", "exif_days", "n_days", "first_shot",
                                "last_shot", "n_duplicates", "n_no_gps", "status", "notes"))
   if (verbose) {
      message(nrow(folders), " folder(s), ", nrow(images), " image(s); ", sum(folders$status == "ok"), " ok, ",
              sum(folders$status == "check"), " to check, ", sum(folders$status == "ignored"), " ignored")
      for (i in which(folders$status != "ok")) message("  ", folders$folder[i], ": ", folders$notes[i])
   }
   list(folders = folders, images = images)
}
