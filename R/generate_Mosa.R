#' Generate orthomosaics
#'
#' @description
#' Implementation in R of a pipeline that performs the Time-SIFT process to time series of drone images. All photos are loaded and aligned in a single block, then orthomosaics and (optionally) DEMs will be generated for each date or flight.
#' Two engines are available: OpenDroneMap (`engine = "odm"`, free and open source, default) and the Metashape python API (`engine = "metashape"`, requires a license).
#'
#' @details
#' The two engines are set to do the same processing as far as possible (same images and dates, same number of
#' keypoints, same accuracy of the GPS positions, depth maps at the same image resolution, colour calibration on the
#' same tie points, products rendered at `resol_ref` in `crs`); the remaining differences come from the algorithms of
#' each software (e.g. ODM detects the keypoints on images of 4480 px at most, and projects the images on a simplified
#' 2.5D mesh instead of a DEM). They are described in `system.file("ENGINES.md", package = "canobsR")` (in French).
#'
#' With `engine = "odm"`:
#' * only RGB images are handled for now (`data_type` and `sun_sensor` are not used);
#' * without `group_by_flight`, the subfolders of `path_in` without a date in their name are ignored;
#' * the folder defines the date: all the images of a folder make one mosaic, even if some were taken on another day
#'   (e.g. a flight finished the next day). The EXIF days are only reported (`check_dates`); use [check_flights()] to
#'   check the folders before processing;
#' * an error is raised if the same image file is found twice (e.g. a copy of the images inside `path_in`);
#' * `downscale_factor_alignement` and `downscale_factor_depth_map` are converted to the ODM levels that use the same
#'   image resolution as Metashape (ODM `--feature-quality` and `--pc-quality`);
#' * `calibrate_col = TRUE` computes one gain per image and per colour channel, and one vignetting correction per
#'   camera and per channel (`calibrate_vignetting`), so that the tie points have the same colour in all the images
#'   where they are seen (as Metashape `calibrateColors(white_balance = TRUE)`), and applies them to the images used
#'   to build the orthomosaics (see `color_gains.csv` and `vignetting.csv` in `out_dir_project`);
#' * after each date, the ODM project is checked: an error is raised if an image of another date is found in it
#'   (see also [check_odm_epochs()]);
#' * the orthomosaics are written in the same format as with Metashape: 3 bands (RGB), white (255) background,
#'   resolution `resol_ref`, coordinate system `crs`. ODM renders them in the UTM zone of the site: when `crs` is this
#'   UTM zone, the pixels are kept as rendered (no resampling; their size, computed by ODM, can differ from
#'   `resol_ref` by a few 1e-5 m); otherwise they are reprojected (bilinear) on a grid aligned on multiples of
#'   `resol_ref`.
#' The ODM projects are written in `out_dir_project` (default: a `_odm_timesift_` folder inside `out_dir_ortho`, deleted at the end);
#' this folder should be on the same disk as the images, which are then hard-linked instead of copied.
#' If a run is interrupted, running the same call again resumes it (set `overwrite = TRUE` to start from scratch).
#'
#' The Time-SIFT method is described in Feurer & Vinatier (2018) \doi{10.1016/j.isprsjprs.2018.10.016}.
#'
#' @param path_in character. Path to the folder where the data is located. Inside this folder, any structure of subfolders containing images is accepted, as long as the date is specified in the folders' name in YYYYMMDD/YYYYMM format. This date is what will be used later to distinct which image belongs to which date when building orthomosaics.
#' @param out_dir_ortho character. Folder where the orthomosaics are saved.
#' @param out_dir_DEM character. Folder where the DEMs are saved. If no path is specified, the DEMs are not saved by default.
#' @param out_dir_project character. Folder where the Metashape project is saved. If no path is specified, the project is not saved by default.
#' @param data_type character. The type of the data used. Either 'RGB' (default) or 'MS' (for multispectral images).
#' @param resol_ref numeric. The resolution (in meters) used to generate DEMs and orthomosaics. Defaults to 0.05.
#' @param crs character. Coordinate system of the orthomosaics, e.g. `"EPSG:32633"`. `"auto"` (default): the WGS 84 / UTM
#'   zone of the site, found from the GPS positions of a few images of each folder.
#' @param site_name character. Adds the data site name into the names of all created folders and files, to better separate generated data from different projects. If not specified, the names will stay generic.
#' @param calibrate_col logical. Calibrates the colours of the images (brightness and white balance, i.e. each colour
#'   channel separately, and vignetting) on the tie points.
#' @param calibrate_col_by character. Scope of the colour calibration, for both engines. Both use the same tie points,
#'   from the alignment of all the dates together; only the images that are made consistent differ:
#'   * "chunk" (default): all the images of all the dates together: a tie point must have the same colour in all the
#'     images, of all the dates. Colours are harmonised inside each mosaic AND between mosaics (part of the colour
#'     changes between dates, e.g. phenology, can be reduced);
#'   * "date": the images of each date together: a tie point must have the same colour in the images of a date, and may
#'     change from one date to another. Colours are harmonised inside each mosaic, and the colour level of each date is
#'     kept (the differences of illumination between dates are kept too).
#'
#'   With `engine = "odm"`, if `out_dir_project` already contains the same run with another colour calibration, only the
#'   texture and the orthomosaic are computed again (alignment, point cloud and mesh are kept): the two options can be
#'   compared on exactly the same geometry. Give another `out_dir_ortho` to keep both series of mosaics.
#' @param sun_sensor logical. Calibrates the reflectance using the sun sensor. Only applies to multispectral images.
#' @param group_by_flight logical. Regroups data by flight (i.e. one orthomosaic for each subfolder of the input folder containing images). If FALSE, regroups it by date (default).
#' @param from_mesh logical. Generates the orthomosaics based on the mesh model rather than the DEM. In this case, the DEM will not be created.
#'   With `engine = "odm"`, the full 3D mesh is used instead of the 2.5D mesh (ODM `--use-3dmesh`).
#' @param downscale_factor_alignement integer. Alignment accuracy (0 - Highest, 1 - High, 2 - Medium, 4 - Low, 8 - Lowest). Defaults to 1.
#' @param downscale_factor_depth_map integer. Depth map quality (1 - Ultra high, 2 - High, 4 - Medium, 8 - Low, 16 - Lowest). Defaults to 2.
#' @param suffix character. Text to add at the end of the output filenames.
#' @param folder_names character. Names of the subfolders of `path_in` to process. By default, all the subfolders with a
#'   date (YYYYMMDD or YYYYMM) in their name (all the subfolders with `group_by_flight = TRUE`); the others are ignored
#'   and listed in a message. Folders whose name starts with "." or "_", and `time-sift-block`, are always ignored.
#' @param engine character. Photogrammetry software used: "odm" (OpenDroneMap, default) or "metashape".
#' @param odm_path character. Only for `engine = "odm"`. With `odm_runner = "native"`, path to the ODM launcher (`run.bat` on Windows, `run.sh` on Linux). By default, the option `canobsR.odm_path`, then the environment variable `ODM_PATH`, then `C:/ODM/run.bat`. With "docker", name of the image (default "opendronemap/odm"). With "apptainer", path to the image file.
#' @param odm_runner character. How ODM is launched: "native" (default), "docker" or "apptainer" (clusters).
#' @param odm_args character vector. Additional options passed to ODM, e.g. `c("--matcher-neighbors", "16")`.
#' @param overwrite logical. Only for `engine = "odm"`. Deletes the existing ODM projects before running, instead of resuming.
#' @param check_dates logical. Only for `engine = "odm"`. Reports the folders whose images were taken on several days or
#'   on another day than the folder date (information only, never an error).
#' @param tempdir_custom character. Only for `engine = "odm"`. Directory for the temporary files (R terra, GDAL and ODM).
#'   Default: the option `canobsR.tempdir` if set (`options(canobsR.tempdir = "D:/tmp")`), otherwise a `_tmp` folder in
#'   `out_dir_project` (or in `out_dir_ortho`). Large temporary files are written during the post-processing of the
#'   orthomosaics: this folder should be on a disk with free space (not the system disk). A subfolder is created in it
#'   and deleted at the end.
#' @param keypoint_limit integer. Number of keypoints detected per image for the alignment (Metashape `keypoint_limit`;
#'   ODM `--min-num-features`, the number of features extracted per image). `NULL` (default): 100000 for Metashape
#'   (its pipeline) and 10000 for ODM. Tests on the canobsR data (3 dates): the co-registration between dates is the
#'   same with 10000, 20000 or 100000 keypoints (2.2-2.3 cm), but ODM agrees locally with Metashape only with 100000
#'   (10 cm against about 38 cm), at the cost of an alignment 23 times longer (16 h instead of 41 min).
#'   With Metashape, the photos that are not aligned are matched again with twice this limit.
#' @param tiepoint_limit integer. Only for `engine = "metashape"`: maximum number of tie points kept per image (Metashape
#'   `tiepoint_limit`, default 10000; doubled for the photos matched again). ODM has no equivalent.
#' @param odm_gpu_fix logical. Only for `engine = "odm"` on Windows. The ODM installer puts a dummy `nvcuda.dll` next to
#'   OpenMVS (`SuperBuild/install/bin`), which hides the NVIDIA driver: OpenMVS then runs on the CPU, 3 to 4 times
#'   slower, without any message. With `TRUE` (default), when an NVIDIA driver is installed, this file is renamed
#'   `nvcuda.dll.odm_dummy` before each run (each ODM installation or update puts it back). If it cannot be renamed
#'   (rights on the ODM folder), or with `FALSE`, a warning is given.
#' @param calibrate_vignetting logical. Only for `engine = "odm"`, with `calibrate_col`: also corrects the vignetting of
#'   the camera, estimated on the same tie points (Metashape `calibrateColors()` estimates it too).
#'
#' @export
#'
#' @return None (metashape) or, invisibly, the paths of the orthomosaics (odm).
#'
#' @examples
#'
#' \dontrun{
#' # With OpenDroneMap (default)
#' generate_Mosa(path_in = "path_to_my_drone_data",
#'                out_dir_ortho = "my_output_folder/ORTHO",
#'                resol_ref = 0.05,
#'                site_name = "Bouamir"
#'                )
#'
#' # With Metashape
#' generate_Mosa(path_in = "path_to_my_drone_data",
#'                out_dir_ortho = "my_output_folder/ORTHO",
#'                #out_dir_DEM = "my_output_folder/DEM",
#'                data_type = "RGB",
#'                resol_ref = 0.05,
#'                site_name = "Bouamir",
#'                crs = "EPSG::32633",
#'                engine = "metashape"
#'                )
#'
#' generate_Mosa(path_in = "path_to_my_drone_data",
#'                out_dir_ortho = "my_output_folder/ORTHO",
#'                #out_dir_DEM = "my_output_folder/DEM",
#'                engine = "metashape",
#'                data_type = "MS",
#'                sun_sensor = TRUE,
#'                crs = "EPSG::32622",
#'                downscale_factor_depth_map = 4,
#'                group_by_flight = TRUE,
#'                suffix = "_ORTHO"
#'                )
#'                }
#'


generate_Mosa <- function(path_in, out_dir_ortho, out_dir_DEM = NULL, out_dir_project = NULL,
                       data_type = "RGB", resol_ref = 0.05, crs = "auto",
                       site_name = "", calibrate_col = TRUE, sun_sensor = FALSE,
                       group_by_flight = FALSE, from_mesh = FALSE,
                       downscale_factor_alignement = 1L, downscale_factor_depth_map = 2L,
                       suffix = "", folder_names = NULL,
                       calibrate_col_by = c("chunk", "date"),
                       engine = c("odm", "metashape"),
                       odm_path = NULL, odm_runner = c("native", "docker", "apptainer"),
                       odm_args = NULL, overwrite = FALSE, check_dates = TRUE,
                       tempdir_custom = getOption("canobsR.tempdir", NULL),
                       keypoint_limit = NULL, tiepoint_limit = 10000L,
                       calibrate_vignetting = TRUE, odm_gpu_fix = TRUE) {

   engine <- match.arg(engine)
   if (is.null(keypoint_limit)) keypoint_limit <- if (engine == "odm") 10000L else 100000L
   calibrate_col_by <- match.arg(calibrate_col_by)
   # same subfolders for both engines: by default the ones with a date (YYYYMMDD or YYYYMM) in their name
   if (is.null(folder_names)) folder_names <- .select_subfolders(path_in, group_by_flight)
   folder_names <- as.list(folder_names)     # a python list even for a single folder
   if (identical(crs, "auto")) crs <- .auto_crs(path_in, folder_names)
   crs <- sub("^EPSG:+", "EPSG::", crs, ignore.case = TRUE)   # form of Metashape; .crs_r() gives the one of sf/terra

   if (engine == "metashape") {
      .check_metashape()
      .canobs_call("timesift_metashape", "Time_SIFT_process",
                           pathDIR = path_in,
                           out_dir_ortho = out_dir_ortho,
                           out_dir_DEM = out_dir_DEM,
                           out_dir_project = out_dir_project,
                           data_type = data_type,
                           resol_ref = resol_ref,
                           crs = crs,
                           site_name = site_name,
                           calibrate_col = calibrate_col,
                           calibrate_col_by = calibrate_col_by,
                           sun_sensor = sun_sensor,
                           group_by_flight = group_by_flight,
                           from_mesh = from_mesh,
                           downscale_factor_alignement = downscale_factor_alignement,
                           downscale_factor_depth_map = downscale_factor_depth_map,
                           suffix = suffix,
                           folder_names = folder_names,
                           keypoint_limit = as.integer(keypoint_limit),
                           tiepoint_limit = as.integer(tiepoint_limit)
                           )
      return(invisible(NULL))
   }

   # ---- engine = "odm" ----
   odm_runner <- match.arg(odm_runner)

   # temporary files (terra, GDAL, ODM) in tempdir_custom, not in the R temporary folder of the system disk
   if (is.null(tempdir_custom)) {
      tempdir_custom <- file.path(if (is.null(out_dir_project)) out_dir_ortho else out_dir_project, "_tmp")
   }
   dir.create(tempdir_custom, recursive = TRUE, showWarnings = FALSE)
   tmp_sub <- normalizePath(tempfile("canobs_", tmpdir = tempdir_custom), winslash = "/", mustWork = FALSE)
   dir.create(tmp_sub)
   old_terra <- terra::terraOptions(print = FALSE)$tempdir
   old_env <- Sys.getenv(c("TMPDIR", "TMP", "TEMP", "CPL_TMPDIR"), unset = NA)
   terra::terraOptions(tempdir = tmp_sub)
   Sys.setenv(TMPDIR = tmp_sub, TMP = tmp_sub, TEMP = tmp_sub, CPL_TMPDIR = tmp_sub)
   on.exit({
      terra::tmpFiles(current = TRUE, orphan = FALSE, old = FALSE, remove = TRUE)
      # the previous folder may have been deleted meanwhile: never leave terra on tmp_sub, deleted below
      terra::terraOptions(tempdir = if (dir.exists(old_terra)) old_terra else tempdir())
      for (v in names(old_env)) if (is.na(old_env[[v]])) Sys.unsetenv(v) else do.call(Sys.setenv, as.list(old_env[v]))
      unlink(tmp_sub, recursive = TRUE)
   }, add = TRUE)
   message("Temporary files in ", tmp_sub)
   if (data_type != "RGB") {
      stop("engine = 'odm' only handles RGB images for now. Use engine = 'metashape' for multispectral images.")
   }
   if (odm_runner == "native") {
      if (is.null(odm_path)) odm_path <- .find_odm(canobs_config()$odm_path)
      if (is.null(odm_path)) stop("ODM not found. Install it, then run canobs_setup() (or give odm_path).", call. = FALSE)
      if (!file.exists(odm_path)) {
         stop("ODM launcher not found: ", odm_path,
              "\nGive the path of run.bat (Windows) or run.sh (Linux) with the 'odm_path' argument.")
      }
      odm_path <- normalizePath(odm_path, winslash = "/")
   }

   # GPU: ODM's dummy nvcuda.dll hides the NVIDIA driver on Windows (OpenMVS then runs on the CPU, 3-4 x slower)
   if (odm_runner == "native") .check_odm_gpu(odm_path, odm_gpu_fix)

   res <- .canobs_call("timesift_odm", "timesift_odm_process",
                                  pathDIR = path_in,
                                  out_dir_ortho = out_dir_ortho,
                                  out_dir_DEM = out_dir_DEM,
                                  out_dir_project = out_dir_project,
                                  resol_ref = resol_ref,
                                  site_name = site_name,
                                  group_by_flight = group_by_flight,
                                  downscale_factor_alignement = as.integer(downscale_factor_alignement),
                                  downscale_factor_depth_map = as.integer(downscale_factor_depth_map),
                                  suffix = suffix,
                                  folder_names = folder_names,
                                  odm_path = odm_path,
                                  odm_runner = odm_runner,
                                  odm_args = as.list(as.character(odm_args)),
                                  overwrite = overwrite,
                                  calibrate_col = calibrate_col,
                                  calibrate_col_by = calibrate_col_by,
                                  calibrate_vignetting = calibrate_vignetting,
                                  keypoint_limit = as.integer(keypoint_limit),
                                  from_mesh = from_mesh,
                                  odm_gpu_fix = FALSE,
                                  check_dates = check_dates,
                                  tmp_dir = tmp_sub
                                  )

   # same format as the Metashape outputs: RGB, white background, resol_ref grid, crs
   target <- gsub("::", ":", crs, fixed = TRUE)
   for (f in unlist(res)) .format_odm_raster(f, target, resol_ref, type = "ortho")
   if (!is.null(out_dir_DEM) && !from_mesh) {
      if (out_dir_DEM == "") out_dir_DEM <- file.path(dirname(out_dir_ortho), "DEM")
      dem <- file.path(out_dir_DEM, basename(unlist(res)))
      for (f in dem[file.exists(dem)]) .format_odm_raster(f, target, resol_ref, type = "dem")
   }

   invisible(unlist(res))
}


# Converts an ODM raster to the format of the Metashape outputs of canobsR
#  - ortho: 3 bands (alpha removed), 255 where there is no data (Metashape white background), INT1U
#  - dem: 1 band, NA where there is no data
#  - both: crs, resolution res.
# ODM renders the products in the UTM zone of the site (WGS84). When it is the target crs and the resolution is
# already res (ODM option --ignore-gsd), the pixels are kept as they are (no resampling, like Metashape, which
# renders the orthomosaic directly in the target crs): only the format changes, and the grid is the one of ODM.
# Otherwise (other crs), the raster is reprojected (bilinear) on a grid aligned on multiples of res.
.format_odm_raster <- function(f, crs, res, type = c("ortho", "dem")) {
   type <- match.arg(type)
   crs <- .crs_r(crs)
   r <- terra::rast(f)
   gdal <- c("COMPRESS=LZW", "BIGTIFF=IF_SAFER", "TILED=YES")
   out <- paste0(tools::file_path_sans_ext(f), "_canobs_tmp.tif")
   # ODM computes its pixel size from the extent and the number of pixels: it differs from res by a few 1e-5
   # (e.g. 0.049998 x 0.049999 m for 0.05). Within 0.1 %, the pixels are kept as they are (relabelling them at
   # exactly res would shift the far edge by several cm; resampling would smooth them).
   resample <- !.same_crs(terra::crs(r), crs) || any(abs(terra::res(r) - res) > 1e-3 * res)
   if (!resample && any(terra::res(r) != res))
      message(basename(f), ": pixels of ODM kept, no resampling (pixel size ",
              paste(signif(terra::res(r), 7), collapse = " x "), " m)")
   if (resample) {
      message("Reprojection of ", basename(f), " (ODM: ", .crs_label(terra::crs(r)), ", ",
              paste(signif(terra::res(r), 4), collapse = " x "), " m) to ", crs, ", ", res, " m (bilinear)")
      # template: target crs, grid aligned on multiples of res
      e <- terra::ext(terra::project(terra::as.polygons(terra::ext(r), crs = terra::crs(r)), crs))
      e <- terra::ext(floor(e$xmin / res) * res, ceiling(e$xmax / res) * res,
                      floor(e$ymin / res) * res, ceiling(e$ymax / res) * res)
      tmpl <- terra::rast(e, resolution = res, crs = crs)
   }
   if (type == "ortho") {
      if (terra::nlyr(r) >= 4) {
         # the alpha band defines the no-data area: a no-data value declared in the file (e.g. 255) is ignored,
         # through a small virtual file (VRT) written in the temporary folder
         vrt <- tempfile(fileext = ".vrt", tmpdir = terra::terraOptions(print = FALSE)$tempdir)
         sf::gdal_utils("translate", f, vrt, options = c("-of", "VRT", "-a_nodata", "none"))
         r <- terra::rast(vrt)[[1:4]]
      } else {
         r <- c(r[[1:3]], terra::init(r[[1]], 255))
      }
      proj <- NULL
      if (resample) {
         # reprojection of the 4 bands (RGB + alpha), written compressed next to the output
         # (INT2U: 0-255 values and a separate no-data value, 65535)
         proj <- paste0(tools::file_path_sans_ext(f), "_canobs_proj.tif")
         terra::project(r, tmpl, method = "bilinear", filename = proj, overwrite = TRUE,
                        wopt = list(datatype = "INT2U", gdal = gdal))
         r <- terra::rast(proj)
      }
      # alpha removed, white (255) where there is no data, as in the Metashape outputs
      white_bg <- function(r, g, b, a) {
         rgb <- round(cbind(r, g, b))
         rgb[is.na(a) | a < 128 | is.na(rgb[, 1]), ] <- 255
         rgb
      }
      terra::lapp(r, white_bg, filename = out, overwrite = TRUE,
                  wopt = list(datatype = "INT1U", gdal = gdal, NAflag = NA, names = c("red", "green", "blue")))
      rm(r)
      if (!is.null(proj)) unlink(proj)
   } else {
      if (resample) {
         terra::project(r, tmpl, method = "bilinear", filename = out, overwrite = TRUE,
                        wopt = list(datatype = "FLT4S", gdal = gdal))
      } else {
         terra::writeRaster(r, out, overwrite = TRUE, datatype = "FLT4S", gdal = gdal)
      }
      rm(r)
   }
   gc()
   file.remove(f)
   file.rename(out, f)
   invisible(f)
}

# Before a long Metashape run: module installed and licence usable (checked again now: it may have changed)
.check_metashape <- function() {
   r <- .canobs_python(c("-m", "canobspy", "info"), echo = FALSE)
   v <- tryCatch(jsonlite::fromJSON(utils::tail(strsplit(r$stdout, "\n")[[1]], 1)), error = function(e) list())
   if (is.null(v$metashape))
      stop("The Metashape Python module is not installed: canobsR::canobs_install_metashape(), or use engine = \"odm\".", call. = FALSE)
   if (!isTRUE(v$metashape_activated))
      stop("No Metashape licence is activated on this computer: canobsR::canobs_activate_metashape(\"<licence key>\"), ",
           "or use engine = \"odm\".", call. = FALSE)
   invisible(v)
}

# GPU check of native ODM on Windows (see odm_gpu_fix): a banner and a warning that cannot be missed
.check_odm_gpu <- function(odm_path, fix = TRUE) {
   g <- tryCatch(.canobs_call("timesift_odm", "check_odm_gpu", odm_path = odm_path, fix = fix, .echo = FALSE), error = function(e) list(status = "error", path = conditionMessage(e)))
   bar <- strrep("=", 90)
   if (identical(g$status, "renamed")) {
      message(bar, "\nGPU: ODM's dummy nvcuda.dll hid the NVIDIA driver. It has been renamed:\n   ", g$path,
              "\nOpenMVS (dense matching) now uses the GPU.\n", bar)
   } else if (identical(g$status, "dummy_present")) {
      msg <- paste0("ODM WILL NOT USE THE GPU (dense matching 3-4 x slower): the dummy file\n   ", g$path,
                    "\nhides the NVIDIA driver. Rename it nvcuda.dll.odm_dummy (administrator rights on the ODM folder),",
                    "\nor run generate_Mosa() with odm_gpu_fix = TRUE from an R session started as administrator.")
      message(bar, "\n", msg, "\n", bar)
      warning(msg, call. = FALSE, immediate. = TRUE)
   }
   invisible(g)
}

# WGS 84 / UTM zone of the site, from the GPS tags of the images
.auto_crs <- function(path_in, folder_names = NULL) {
   u <- .canobs_call("timesift_odm", "utm_from_images", path_in = normalizePath(path_in, winslash = "/"),
                     folder_names = folder_names, .echo = FALSE)
   message("crs: EPSG:", u$epsg, " (WGS 84 / UTM zone ", u$zone, u$hemisphere, "), from the GPS positions of ",
           u$n_images, " images (median lat ", u$lat, ", lon ", u$lon, ")")
   if (length(u$zones) > 1)
      warning("The images span several UTM zones (EPSG ", paste(unlist(u$zones), collapse = ", "), "): EPSG:", u$epsg,
              " is used. Give crs to choose another one.", call. = FALSE, immediate. = TRUE)
   paste0("EPSG::", u$epsg)
}

# crs written for Metashape ("EPSG::32633") -> form read by sf / terra ("EPSG:32633")
.crs_r <- function(x) if (is.character(x)) sub("^EPSG::", "EPSG:", x, ignore.case = TRUE) else x

# TRUE if the two coordinate reference systems are the same (e.g. ODM "+proj=utm +zone=33 +datum=WGS84" and "EPSG:32633")
.same_crs <- function(a, b) {
   isTRUE(tryCatch(suppressWarnings(sf::st_crs(.crs_r(a)) == sf::st_crs(.crs_r(b))), error = function(e) FALSE))
}

.crs_label <- function(x) {
   cr <- tryCatch(sf::st_crs(x), error = function(e) NULL)
   if (is.null(cr) || is.na(cr$input)) return("unknown crs")
   if (!is.null(cr$epsg) && !is.na(cr$epsg)) paste0("EPSG:", cr$epsg) else cr$input
}


#' Check that the ODM projects of a Time-SIFT run do not mix dates
#'
#' @description
#' Checks, for each date (epoch) of a Time-SIFT run made with `generate_Mosa(engine = "odm")`, that the ODM project of
#' this date only refers to images of this date. All the files that list images are read: image folders, OpenSfM
#' reconstructions, list of cameras used for the texturing (orthomosaic), OpenMVS scene, tracks, ODM report.
#' The images are renamed `<date>__<original name>` by `generate_Mosa()`, so the date of every image is known.
#'
#' `generate_Mosa()` already runs this check after each date and stops if a problem is found.
#'
#' @param out_dir_project character. The `out_dir_project` folder given to `generate_Mosa()`.
#' @param verbose logical. Prints the result for each date.
#'
#' @return A data.frame with one row per date: `ok` is FALSE if an image of another date was found in a file that
#' decides which images are used for the point cloud, the texture or the orthomosaic (`critical`); `other` lists the
#' other files (sparse points, GSD estimate, report) where images of other dates were found. The details are also
#' written in `epochs_check.json` in `out_dir_project`.
#' @export
#'
#' @examples
#' \dontrun{
#' check_odm_epochs("D:/my_data/2_odm_projects")
#' }
check_odm_epochs <- function(out_dir_project, verbose = TRUE) {
   res <- .canobs_call("timesift_odm", "verify_projects", workdir = normalizePath(out_dir_project, winslash = "/"),
                       verbose = verbose)
   data.frame(epoch = vapply(res, function(x) x$epoch, ""),
              ok = vapply(res, function(x) isTRUE(x$ok), TRUE),
              files_checked = vapply(res, function(x) length(x$checked), 0L),
              critical = vapply(res, function(x) paste(names(x$critical), collapse = "; "), ""),
              other = vapply(res, function(x) paste(names(x$other), collapse = "; "), ""))
}


# Subfolders of path_in processed by generate_Mosa() (same rule for both engines)
.select_subfolders <- function(path_in, group_by_flight = FALSE) {
   subs <- list.dirs(path_in, full.names = FALSE, recursive = FALSE)
   keep <- subs[!subs %in% c("time-sift-block", "timesift_block") & !grepl("^[._]", subs)]
   if (!group_by_flight) keep <- keep[grepl("\\d{4}(0[1-9]|1[0-2])([0-2][0-9]|3[01])?", keep)]
   ignored <- setdiff(subs, keep)
   if (length(ignored)) message("Subfolders ignored (no date in their name, or reserved name): ", paste(ignored, collapse = ", "))
   if (length(keep) == 0) stop("No subfolder to process in ", path_in)
   keep
}
