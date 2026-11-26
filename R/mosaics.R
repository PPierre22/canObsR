# Long series of drone flights: processing plan (chunks of dates) and generation of the orthomosaics, chunk by
# chunk, with a journal that allows to resume where a run stopped.

.flights_table <- function(flights_dir) {
   fl <- .canobs_call("flights", "list_flights", flights_dir = normalizePath(flights_dir, winslash = "/"), .echo = FALSE)
   if (!length(fl)) stop("No subfolder in ", flights_dir, call. = FALSE)
   data.frame(folder = vapply(fl, function(x) x$folder, ""),
              date = vapply(fl, function(x) if (is.null(x$date)) NA_character_ else x$date, ""),
              drone = vapply(fl, function(x) x$drone, ""),
              epoch = vapply(fl, function(x) if (is.null(x$epoch)) NA_character_ else x$epoch, ""),
              n_images = vapply(fl, function(x) as.integer(x$n_images), 0L),
              stringsAsFactors = FALSE)
}

.drone_label <- function(d) ifelse(nzchar(d), d, "main drone")

# x split into ceiling(length(x) / size) groups of nearly equal sizes: round robin (random order) or consecutive
.split_even <- function(x, size, consecutive = FALSE) {
   k <- ceiling(length(x) / size)
   if (consecutive) split(x, cut(seq_along(x), k, labels = FALSE)) else split(x, rep_len(seq_len(k), length(x)))
}

#' Plan the processing of a long series of flights in chunks of dates
#'
#' @description
#' Time-SIFT processes several dates together (one photogrammetric block), which makes their mosaics consistent
#' with each other; but a block of 100 dates would not fit in memory. The dates of a site are therefore processed by
#' chunks of `chunk_size` dates. `plan_mosaics()` decides these chunks and writes them in `plan.csv`, in `out_dir`.
#' [generate_mosaics()] follows this plan (it calls `plan_mosaics()` itself if there is no plan yet).
#'
#' * **Drones**: by default, a chunk only contains flights of the same drone (cameras with different colours and
#'   geometry in one block degrade the colour calibration and the matching). `split_drones = FALSE` allows mixed
#'   chunks (with a warning).
#' * **Order**: `"random"` (default) spreads the dates of each chunk over the whole period (seasons, years), so that
#'   every chunk contains varied conditions; `"chronological"` makes chunks of consecutive dates.
#' * **New flights**: when the plan already exists, the chunks already planned are kept as they are, and the new
#'   dates make new chunks. The plan can also be edited by hand (column `chunk`) before running [generate_mosaics()].
#'
#' The flight folders are named `<prefix>_<YYYYMMDD>[_<drone>]`, e.g. `RGB_20250101` (main drone of the site) and
#' `RGB_20250115_Matrice4E`. A mosaic is made for each epoch: the date, followed by the drone if the folder names
#' one (`20250101`, `20250115_Matrice4E`).
#'
#' @param flights_dir character. Folder with one subfolder per flight (see [check_flights()]).
#' @param out_dir character. Output folder of the series (the plan is written in `out_dir/plan.csv`).
#' @param chunk_size integer. Maximum number of dates per chunk.
#' @param split_drones logical. Never mix drones in a chunk.
#' @param order character. `"random"` or `"chronological"`.
#' @param seed integer. Seed of the random order (same plan when run again).
#' @param overwrite logical. Plans again all the dates (the chunks already planned are not kept).
#' @return Invisibly, the plan: a data.frame with one row per flight folder (`chunk`, `epoch`, `date`, `drone`,
#'   `folder`, `n_images`).
#' @export
#' @examples
#' \dontrun{
#' plan <- plan_mosaics("D:/bouamir/1_flights", "D:/bouamir/2_mosaics", chunk_size = 8)
#' table(plan$chunk)
#' }
plan_mosaics <- function(flights_dir, out_dir, chunk_size = 10L, split_drones = TRUE,
                         order = c("random", "chronological"), seed = 1L, overwrite = FALSE) {
   order <- match.arg(order)
   dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
   f_plan <- file.path(out_dir, "plan.csv")
   fl <- .flights_table(flights_dir)

   ign <- fl$folder[is.na(fl$epoch)]
   if (length(ign)) message("Ignored (no date in the name, or reserved name): ", paste(ign, collapse = ", "))
   empty <- fl$folder[!is.na(fl$epoch) & fl$n_images == 0]
   if (length(empty)) warning("Folders without images, ignored: ", paste(empty, collapse = ", "), call. = FALSE)
   fl <- fl[!is.na(fl$epoch) & fl$n_images > 0, ]
   if (!nrow(fl)) stop("No flight folder with images in ", flights_dir, call. = FALSE)

   old <- NULL
   if (file.exists(f_plan) && !overwrite) {
      old <- utils::read.csv(f_plan, colClasses = "character")
      gone <- setdiff(old$folder, fl$folder)
      if (length(gone)) warning("In plan.csv but not found in ", flights_dir, ": ", paste(gone, collapse = ", "),
                                call. = FALSE)
      old <- old[old$folder %in% fl$folder, c("chunk", "folder")]
   }
   fl$chunk <- if (is.null(old)) NA_character_ else old$chunk[match(fl$folder, old$folder)]
   # a folder of an epoch already planned joins its chunk
   for (i in which(is.na(fl$chunk))) {
      same <- fl$chunk[fl$epoch == fl$epoch[i] & !is.na(fl$chunk)]
      if (length(same)) fl$chunk[i] <- same[1]
   }

   new <- unique(fl$epoch[is.na(fl$chunk)])
   if (length(new)) {
      ep <- unique(fl[fl$epoch %in% new, c("epoch", "date", "drone")])
      groups <- if (split_drones) split(ep, ep$drone) else list(all = ep)
      used <- suppressWarnings(as.integer(sub("^chunk_", "", stats::na.omit(fl$chunk))))
      next_id <- if (length(used) && any(!is.na(used))) max(used, na.rm = TRUE) + 1L else 1L
      if (!split_drones && length(unique(ep$drone)) > 1)
         warning("split_drones = FALSE: chunks can mix drones (", paste(.drone_label(unique(ep$drone)), collapse = ", "),
                 "). Their colours and cameras differ: the colour calibration and the matching of the block may be ",
                 "worse than with one drone per chunk.", call. = FALSE, immediate. = TRUE)
      if (order == "random") set.seed(seed)
      for (g in groups) {
         e <- if (order == "random") g$epoch[sample.int(nrow(g))] else g$epoch[base::order(g$date, g$epoch)]
         parts <- .split_even(e, chunk_size, consecutive = order == "chronological")
         for (p in parts) {
            fl$chunk[fl$epoch %in% p] <- sprintf("chunk_%03d", next_id)
            next_id <- next_id + 1L
         }
      }
   }
   plan <- fl[base::order(fl$chunk, fl$date, fl$epoch), c("chunk", "epoch", "date", "drone", "folder", "n_images")]
   rownames(plan) <- NULL
   utils::write.csv(plan, f_plan, row.names = FALSE)

   ch <- split(plan, plan$chunk)
   message(length(unique(plan$epoch)), " dates (", sum(plan$n_images), " images) in ", length(ch), " chunks",
           if (length(new) && !is.null(old)) paste0(" (", length(new), " new dates)"), "; plan: ", f_plan)
   for (d in unique(plan$drone)) {
      sub <- plan[plan$drone == d, ]
      message("  ", .drone_label(d), ": ", length(unique(sub$epoch)), " dates, ", sum(sub$n_images), " images, ",
              length(unique(sub$chunk)), " chunk(s)")
   }
   sizes <- vapply(ch, function(x) length(unique(x$epoch)), 0L)
   message("  dates per chunk: ", paste(range(sizes), collapse = " to "))
   invisible(plan)
}

# ---- generate_mosaics -----------------------------------------------------------------------------------

.mosaic_options <- list(
   # plan
   chunk_size = 10L, split_drones = TRUE, order = "random", seed = 1L,
   # chunks to run, errors, files
   chunks = NULL, on_error = "continue", keep_projects = FALSE, project_dir = NULL, stage_dir = NULL,
   site_name = "", overwrite = FALSE,
   # photogrammetry (see generate_Mosa)
   keypoint_limit = NULL, tiepoint_limit = 10000L, calibrate_col = TRUE, calibrate_col_by = "chunk",
   calibrate_vignetting = TRUE, from_mesh = FALSE, downscale_factor_alignement = 1L, downscale_factor_depth_map = 2L,
   data_type = "RGB", sun_sensor = FALSE, check_dates = TRUE,
   odm_path = NULL, odm_runner = "native", odm_args = NULL, odm_gpu_fix = TRUE,
   # resources
   max_memory_gb = "auto", monitor = FALSE, parallel_epochs = "auto")

.check_options <- function(dots) {
   bad <- setdiff(names(dots), names(.mosaic_options))
   if (length(bad) || any(!nzchar(names(dots))))
      stop("Unknown option(s): ", paste(bad, collapse = ", "), ". Options: ",
           paste(names(.mosaic_options), collapse = ", "), " (see ?generate_mosaics).", call. = FALSE)
   utils::modifyList(.mosaic_options, dots, keep.null = TRUE)
}

.bar <- function(done, total, width = 30) {
   k <- if (total > 0) round(width * done / total) else 0
   paste0("[", strrep("#", k), strrep("-", width - k), "] ", done, "/", total)
}

.fmt_min <- function(m) if (is.na(m)) "?" else if (m < 90) sprintf("%.0f min", m) else sprintf("%.1f h", m / 60)

.read_journal <- function(f) {
   if (!file.exists(f)) return(data.frame(chunk = character(0), epoch = character(0), status = character(0),
                                          started = character(0), minutes = numeric(0), file = character(0),
                                          message = character(0), stringsAsFactors = FALSE))
   utils::read.csv(f, colClasses = c(minutes = "numeric"), stringsAsFactors = FALSE)
}

#' Orthomosaics of a long series of drone flights
#'
#' @description
#' Makes one orthomosaic per date (and drone) for all the flights of a site, with the Time-SIFT method, chunk of
#' dates by chunk of dates (see [plan_mosaics()]), with OpenDroneMap (default) or Agisoft Metashape.
#'
#' Designed for long series (100 dates and more, hundreds of hectares at 5 cm) on a single computer:
#' * **resume**: each mosaic is recorded in `out_dir/journal.csv` as soon as it is made. Run the same command again
#'   after an interruption (crash, power cut, Esc): the mosaics already made are skipped, and an interrupted chunk
#'   restarts from its last finished step;
#' * **errors**: by default, a chunk that fails is recorded with its error and the next chunk starts
#'   (`on_error = "stop"` stops). Run the command again to retry the failed chunks only;
#' * **new flights**: add the new flight folders and run the command again: they are planned in new chunks;
#' * **progress and report**: progress of the chunks with an estimated remaining time; a report at the end; the full
#'   output of each chunk in `out_dir/logs/`;
#' * **disk space**: the images are not copied. ODM needs them in its project folder: they are hard-linked there
#'   (no disk space used) if the project folder is on the same disk as the images, otherwise copied, one chunk at a
#'   time. Each project is deleted once its mosaics are made (option `keep_projects`).
#'
#' Output folder: `orthomosaics/` (one GeoTIFF per epoch, e.g. `20250101.tif`, `20250115_Matrice4E.tif`),
#' `dem/` (if `keep_dem`), `plan.csv`, `journal.csv`, `settings.json`, `logs/`, `projects/` (during processing).
#'
#' @param flights_dir character. Folder with one subfolder per flight, named `<prefix>_<YYYYMMDD>[_<drone>]` (see
#'   [check_flights()] and [plan_mosaics()]).
#' @param out_dir character. Output folder of the series.
#' @param engine character. `"odm"` (OpenDroneMap, default) or `"metashape"` (licence needed).
#' @param resolution numeric. Pixel size of the mosaics, in metres.
#' @param crs character. Coordinate reference system of the mosaics, e.g. `"EPSG:32633"`. `"auto"` (default): the
#'   WGS 84 / UTM zone of the site, from the GPS positions of the images.
#' @param keep_dem logical. Also saves the elevation models (DSM), in `out_dir/dem` (ODM: computing time + 10-20 %).
#' @param tmp_dir character. Folder for the temporary files (tens of GB for large sites): on a disk with space.
#'   Default: `out_dir/_tmp`.
#' @param n_cores Threads of ODM: an integer, or `"auto"` (default): the processors minus a few left free for the
#'   computer, within the free memory: the SfM of a chunk needs about 1.1 GB per thread (feature detection), the
#'   processing of each date much less (e.g. laptop with 28 processors and 18 GB free: 14 and 25 threads). The
#'   choices are printed. Metashape
#'   manages its processors itself.
#' @param ... Advanced options (an unknown name is an error):
#'   * plan (used when the plan is made, see [plan_mosaics()]): `chunk_size` (10), `split_drones` (TRUE),
#'     `order` ("random"), `seed` (1);
#'   * run: `chunks` (ids of the chunks to process, default all), `on_error` ("continue" or "stop"),
#'     `keep_projects` (FALSE), `project_dir` (folder of the photogrammetric projects, default `out_dir/projects`;
#'     on the disk of the images for hard links), `stage_dir` (NULL: if given, the images of each chunk are first
#'     copied there, then deleted: only useful when the images are on a slow network drive), `site_name` ("",
#'     prefix of the file names), `overwrite` (FALSE: TRUE makes again the mosaics already made);
#'   * photogrammetry, see [generate_Mosa()]: `keypoint_limit` (ODM 10000, Metashape 100000), `tiepoint_limit`,
#'     `calibrate_col`, `calibrate_col_by`, `calibrate_vignetting`, `from_mesh`, `downscale_factor_alignement`,
#'     `downscale_factor_depth_map`, `data_type`, `sun_sensor`, `check_dates`, `odm_path`, `odm_runner`,
#'     `odm_args`, `odm_gpu_fix`;
#'   * resources: `max_memory_gb` (memory ODM may use, for `n_cores = "auto"`; default 75 % of the RAM),
#'     `monitor` (FALSE: TRUE records the use of the processors, RAM and GPU every 5 s in
#'     `logs/resources_<chunk>.csv`, and prints a table per step of ODM), `parallel_epochs` ("auto": dates of a
#'     chunk processed at the same time after its SfM, as many as the free memory allows, see [generate_Mosa()]).
#' @return Invisibly, the journal (data.frame, one row per epoch: `chunk`, `epoch`, `status`, `started`, `minutes`,
#'   `file`, `message`).
#' @export
#' @examples
#' \dontrun{
#' generate_mosaics("D:/bouamir/1_flights", "D:/bouamir/2_mosaics")
#' # later, after new flights, or after an interruption: the same command
#' generate_mosaics("D:/bouamir/1_flights", "D:/bouamir/2_mosaics")
#' # advanced options
#' generate_mosaics("D:/bouamir/1_flights", "D:/bouamir/2_mosaics", keep_dem = TRUE,
#'                  tmp_dir = "E:/tmp", chunk_size = 6, keypoint_limit = 20000)
#' }
generate_mosaics <- function(flights_dir, out_dir, engine = c("odm", "metashape"), resolution = 0.05,
                             crs = "auto", keep_dem = FALSE, tmp_dir = NULL, n_cores = "auto", ...) {
   engine <- match.arg(engine)
   opt <- .check_options(list(...))
   opt$on_error <- match.arg(opt$on_error, c("continue", "stop"))
   if (!dir.exists(flights_dir)) stop("Folder not found: ", flights_dir, call. = FALSE)
   if (is.null(canobs_config())) stop("Run canobsR::canobs_setup() first.", call. = FALSE)
   flights_dir <- normalizePath(flights_dir, winslash = "/")
   dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
   out_dir <- normalizePath(out_dir, winslash = "/")
   d_ortho <- file.path(out_dir, "orthomosaics"); d_dem <- file.path(out_dir, "dem"); d_log <- file.path(out_dir, "logs")
   d_proj <- if (is.null(opt$project_dir)) file.path(out_dir, "projects") else opt$project_dir
   if (is.null(tmp_dir)) tmp_dir <- file.path(out_dir, "_tmp")
   for (d in c(d_ortho, d_log, d_proj, tmp_dir, if (keep_dem) d_dem)) dir.create(d, recursive = TRUE, showWarnings = FALSE)
   f_journal <- file.path(out_dir, "journal.csv"); f_set <- file.path(out_dir, "settings.json")
   t_start <- Sys.time()
   rule <- strrep("=", 100)
   message(rule, "\ngenerate_mosaics(): ", flights_dir, "\n  -> ", out_dir, "\n", rule)

   # settings: the crs is decided once for the whole series; a change of settings is reported
   set <- if (file.exists(f_set)) jsonlite::read_json(f_set, simplifyVector = TRUE) else NULL
   if (identical(crs, "auto")) crs <- if (!is.null(set$crs)) set$crs else sub("::", ":", .auto_crs(flights_dir), fixed = TRUE)
   new_set <- list(engine = engine, resolution = resolution, crs = sub("::", ":", crs, fixed = TRUE), keep_dem = keep_dem,
                   keypoint_limit = opt$keypoint_limit, calibrate_col = opt$calibrate_col,
                   calibrate_col_by = opt$calibrate_col_by, calibrate_vignetting = opt$calibrate_vignetting,
                   from_mesh = opt$from_mesh, canobsR = as.character(utils::packageVersion("canobsR")))
   if (!is.null(set)) {
      ch <- names(new_set)[!mapply(identical, lapply(new_set, as.character), lapply(set[names(new_set)], as.character))]
      ch <- setdiff(ch, "canobsR")
      if (length(ch)) warning("Settings changed since the first run (", paste(ch, collapse = ", "), "): the mosaics ",
                              "already made keep the previous ones (overwrite = TRUE to make them again).",
                              call. = FALSE, immediate. = TRUE)
   }
   jsonlite::write_json(new_set, f_set, auto_unbox = TRUE, pretty = TRUE, null = "null")
   message("engine ", engine, ", ", resolution, " m, ", new_set$crs, if (keep_dem) ", with DSM")

   # plan (updated with the new flights) and journal
   plan <- plan_mosaics(flights_dir, out_dir, chunk_size = opt$chunk_size, split_drones = opt$split_drones,
                        order = opt$order, seed = opt$seed)
   jr <- .read_journal(f_journal)
   ep <- unique(plan[, c("chunk", "epoch")])
   ep$file <- file.path(d_ortho, paste0(if (nzchar(opt$site_name)) paste0(opt$site_name, "_"), ep$epoch, ".tif"))
   is_done <- function(e) {
      j <- jr[jr$epoch == e & jr$status == "done", ]
      nrow(j) > 0 && file.exists(j$file[nrow(j)])
   }
   ep$done <- !opt$overwrite & vapply(ep$epoch, is_done, TRUE)
   chunks <- unique(ep$chunk)
   if (!is.null(opt$chunks)) chunks <- intersect(chunks, opt$chunks)
   todo <- chunks[vapply(chunks, function(k) any(!ep$done[ep$chunk == k]), TRUE)]
   message(sum(ep$done), "/", nrow(ep), " mosaics already made; ", length(todo), " chunk(s) to process")

   n_img <- tapply(plan$n_images, plan$chunk, sum)
   t_img <- NA_real_                                        # minutes per image, from the chunks of this run
   for (i in seq_along(todo)) {
      k <- todo[i]
      rows <- plan[plan$chunk == k, ]
      left <- sum(n_img[todo[i:length(todo)]])
      message("\n", rule, "\n", .bar(i - 1, length(todo)), " chunks   ", k, ": ", length(unique(rows$epoch)), " dates, ",
              sum(rows$n_images), " images (", paste(unique(.drone_label(rows$drone)), collapse = ", "), ")",
              "   remaining time: ", .fmt_min(t_img * left), "\n", rule)
      log <- file.path(d_log, paste0(k, ".log"))
      cat(format(Sys.time()), k, "\n", file = log, append = TRUE)
      old <- options(canobsR.call_log = log)
      t0 <- Sys.time()
      src <- flights_dir; proj <- file.path(d_proj, k)
      if (!is.null(opt$stage_dir)) {
         src <- file.path(opt$stage_dir, "flights"); proj <- file.path(opt$stage_dir, "projects", k)
         unlink(src, recursive = TRUE); dir.create(src, recursive = TRUE)
         message("copy of the images of ", k, " to ", src, "...")
         for (f in unique(rows$folder)) file.copy(file.path(flights_dir, f), src, recursive = TRUE)
      }
      err <- tryCatch({
         generate_Mosa(path_in = src, out_dir_ortho = d_ortho, out_dir_DEM = if (keep_dem) d_dem else NULL,
                       out_dir_project = proj, data_type = opt$data_type, resol_ref = resolution, crs = crs,
                       site_name = opt$site_name, calibrate_col = opt$calibrate_col, sun_sensor = opt$sun_sensor,
                       from_mesh = opt$from_mesh, downscale_factor_alignement = opt$downscale_factor_alignement,
                       downscale_factor_depth_map = opt$downscale_factor_depth_map, folder_names = unique(rows$folder),
                       calibrate_col_by = opt$calibrate_col_by, engine = engine, odm_path = opt$odm_path,
                       odm_runner = opt$odm_runner, odm_args = opt$odm_args, overwrite = opt$overwrite,
                       check_dates = opt$check_dates, tempdir_custom = tmp_dir, keypoint_limit = opt$keypoint_limit,
                       tiepoint_limit = opt$tiepoint_limit, calibrate_vignetting = opt$calibrate_vignetting,
                       odm_gpu_fix = opt$odm_gpu_fix, n_cores = n_cores, max_memory_gb = opt$max_memory_gb,
                       monitor_file = if (isTRUE(opt$monitor)) file.path(d_log, paste0("resources_", k, ".csv")),
                       parallel_epochs = opt$parallel_epochs)
         NULL
      }, error = function(e) conditionMessage(e), interrupt = function(e) "interrupted")
      options(old)
      mins <- as.numeric(difftime(Sys.time(), t0, units = "mins"))
      e_k <- ep[ep$chunk == k, ]
      ok <- file.exists(e_k$file)
      jr <- jr[!jr$epoch %in% e_k$epoch, ]
      jr <- rbind(jr, data.frame(chunk = k, epoch = e_k$epoch, status = ifelse(ok, "done", "failed"),
                                 started = format(t0), minutes = round(mins, 1), file = e_k$file,
                                 message = if (is.null(err)) ifelse(ok, "", "mosaic not produced") else gsub("[\r\n]+", " ", err),
                                 stringsAsFactors = FALSE))
      utils::write.csv(jr, f_journal, row.names = FALSE)
      if (!is.null(opt$stage_dir)) unlink(src, recursive = TRUE)
      if (all(ok)) {
         if (!opt$keep_projects) unlink(proj, recursive = TRUE)
         t_img <- if (is.na(t_img)) mins / sum(rows$n_images) else (t_img + mins / sum(rows$n_images)) / 2
         message("[OK] ", k, ": ", sum(ok), " mosaic(s) in ", .fmt_min(mins))
      } else {
         message("[!!] ", k, ": ", sum(!ok), " mosaic(s) not made: ", if (is.null(err)) "see the log" else err,
                 "\n     log: ", log, "\n     the project is kept: running the command again resumes it")
         if (identical(err, "interrupted")) stop("Interrupted: run the same command again to resume.", call. = FALSE)
         if (opt$on_error == "stop") stop("Chunk ", k, " failed (on_error = \"stop\").", call. = FALSE)
      }
   }

   # report
   jr <- .read_journal(f_journal)
   jr <- jr[jr$epoch %in% ep$epoch, ]
   n_ok <- sum(jr$status == "done"); failed <- jr[jr$status != "done", ]
   message("\n", rule, "\nReport: ", n_ok, "/", nrow(ep), " mosaics made", if (nrow(failed)) paste0(", ", nrow(failed), " failed"),
           " (", .fmt_min(as.numeric(difftime(Sys.time(), t_start, units = "mins"))), " for this run)")
   if (nrow(failed)) {
      for (k in unique(failed$chunk)) message("  [!!] ", k, ": ", paste(failed$epoch[failed$chunk == k], collapse = ", "),
                                               "\n       ", failed$message[failed$chunk == k][1])
      message("  Run the same command again to retry them (the mosaics made are kept).")
   }
   missing <- setdiff(ep$epoch, jr$epoch)
   if (length(missing)) message("  not processed in this run: ", length(missing), " dates (option chunks)")
   message("  mosaics: ", d_ortho, "\n  journal:  ", f_journal, "\n", rule)
   invisible(jr)
}
