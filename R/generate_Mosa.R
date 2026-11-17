#' Generate orthomosaics
#'
#' @description
#' Implementation in R of a pipeline that performs the Time-SIFT process to time series of drone images. All photos are loaded and aligned in a single block, then orthomosaics and (optionally) DEMs will be generated for each date or flight.
#' Two engines are available: OpenDroneMap (`engine = "odm"`, free and open source, default) and the Metashape python API (`engine = "metashape"`, requires a license).
#'
#' @details
#' With `engine = "odm"`:
#' * only RGB images are handled for now (`data_type`, `sun_sensor` and `from_mesh` are not used);
#' * without `group_by_flight`, the subfolders of `path_in` without a date in their name are ignored, and an error is
#'   raised if an image is found twice, or if the EXIF date of an image does not match the date of its folder (`check_dates`);
#' * `downscale_factor_alignement` and `downscale_factor_depth_map` are converted to the ODM levels that use the same
#'   image resolution as Metashape (ODM `--feature-quality` and `--pc-quality`);
#' * `calibrate_col = TRUE` computes one gain per image and per colour channel so that the tie points of the whole
#'   multi-temporal block (all dates together, as in the Metashape pipeline) have the same colour in all images, and
#'   applies it to the images used to build the orthomosaics (see `color_gains.csv` in `out_dir_project`);
#' * after each date, the ODM project is checked: an error is raised if an image of another date is found in it
#'   (see also [check_odm_epochs()]);
#' * the orthomosaics are written in the same format as with Metashape: 3 bands (RGB), white (255) background,
#'   resolution `resol_ref` on a grid aligned on multiples of `resol_ref`, coordinate system `crs`.
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
#' @param crs character. Coordinate system used for the orthomosaic, in a string format. Example: crs="EPSG::32622" (default value).
#' @param site_name character. Adds the data site name into the names of all created folders and files, to better separate generated data from different projects. If not specified, the names will stay generic.
#' @param calibrate_col logical. Calibrates the colours of the images (brightness and white balance) on the tie points of all dates.
#' @param sun_sensor logical. Calibrates the reflectance using the sun sensor. Only applies to multispectral images.
#' @param group_by_flight logical. Regroups data by flight (i.e. one orthomosaic for each subfolder of the input folder containing images). If FALSE, regroups it by date (default).
#' @param from_mesh logiclal. Generates the orthomosaics based on the mesh model rather than the DEM. In this case, the DEM will not be created.
#' @param downscale_factor_alignement integer. Alignment accuracy (0 - Highest, 1 - High, 2 - Medium, 4 - Low, 8 - Lowest). Defaults to 1.
#' @param downscale_factor_depth_map integer. Depth map quality (1 - Ultra high, 2 - High, 4 - Medium, 8 - Low, 16 - Lowest). Defaults to 2.
#' @param suffix character. Text to add at the end of the output filenames.
#' @param folder_names character. Names of the subfolders of `path_in` to process. All subfolders by default.
#' @param engine character. Photogrammetry software used: "odm" (OpenDroneMap, default) or "metashape".
#' @param odm_path character. Only for `engine = "odm"`. With `odm_runner = "native"`, path to the ODM launcher (`run.bat` on Windows, `run.sh` on Linux). By default, the option `canObsR.odm_path`, then the environment variable `ODM_PATH`, then `C:/ODM/run.bat`. With "docker", name of the image (default "opendronemap/odm"). With "apptainer", path to the image file.
#' @param odm_runner character. How ODM is launched: "native" (default), "docker" or "apptainer" (clusters).
#' @param odm_args character vector. Additional options passed to ODM, e.g. `c("--matcher-neighbors", "16")`.
#' @param overwrite logical. Only for `engine = "odm"`. Deletes the existing ODM projects before running, instead of resuming.
#' @param check_dates logical. Only for `engine = "odm"`. Checks that the EXIF date of every image matches the date of its folder.
#'
#' @export
#' @importFrom reticulate source_python
#'
#' @return None (metashape) or, invisibly, the paths of the orthomosaics (odm).
#'
#' @examples
#'
#' \dontrun{
#' library(reticulate)
#'
#' # With OpenDroneMap (default)
#' generate_Mosa(path_in = "path_to_my_drone_data",
#'                out_dir_ortho = "my_output_folder/ORTHO",
#'                resol_ref = 0.05,
#'                site_name = "Bouamir",
#'                crs = "EPSG::32633",
#'                odm_path = "C:/ODM/run.bat"
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
                       data_type = "RGB", resol_ref = 0.05, crs = "EPSG::32622",
                       site_name = "", calibrate_col = TRUE, sun_sensor = FALSE,
                       group_by_flight = FALSE, from_mesh = FALSE,
                       downscale_factor_alignement = 1L, downscale_factor_depth_map = 2L,
                       suffix = "", folder_names = NULL,
                       engine = c("odm", "metashape"),
                       odm_path = NULL, odm_runner = c("native", "docker", "apptainer"),
                       odm_args = NULL, overwrite = FALSE, check_dates = TRUE) {

   engine <- match.arg(engine)
   py <- new.env()

   if (engine == "metashape") {

      reticulate::source_python(system.file("PYTHON/TimeSIFT_scripts_auto.py", package = "canObsR"), envir = py)

      py$Time_SIFT_process(pathDIR = path_in,
                           out_dir_ortho = out_dir_ortho,
                           out_dir_DEM = out_dir_DEM,
                           out_dir_project = out_dir_project,
                           data_type = data_type,
                           resol_ref = resol_ref,
                           crs = crs,
                           site_name = site_name,
                           calibrate_col = calibrate_col,
                           sun_sensor = sun_sensor,
                           group_by_flight = group_by_flight,
                           from_mesh = from_mesh,
                           downscale_factor_alignement = downscale_factor_alignement,
                           downscale_factor_depth_map = downscale_factor_depth_map,
                           suffix = suffix,
                           folder_names = folder_names
                           )
      return(invisible(NULL))
   }

   # ---- engine = "odm" ----
   odm_runner <- match.arg(odm_runner)
   if (data_type != "RGB") {
      stop("engine = 'odm' only handles RGB images for now. Use engine = 'metashape' for multispectral images.")
   }
   if (odm_runner == "native") {
      if (is.null(odm_path)) odm_path <- getOption("canObsR.odm_path", Sys.getenv("ODM_PATH", "C:/ODM/run.bat"))
      if (!file.exists(odm_path)) {
         stop("ODM launcher not found: ", odm_path,
              "\nGive the path of run.bat (Windows) or run.sh (Linux) with the 'odm_path' argument.")
      }
      odm_path <- normalizePath(odm_path, winslash = "/")
   }

   reticulate::source_python(system.file("PYTHON/timesift_odm.py", package = "canObsR"), envir = py)

   res <- py$timesift_odm_process(pathDIR = path_in,
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
                                  check_dates = check_dates
                                  )

   # same format as the Metashape outputs: RGB, white background, resol_ref grid, crs
   target <- gsub("::", ":", crs, fixed = TRUE)
   for (f in unlist(res)) .format_odm_raster(f, target, resol_ref, type = "ortho")
   if (!is.null(out_dir_DEM)) {
      if (out_dir_DEM == "") out_dir_DEM <- file.path(dirname(out_dir_ortho), "DEM")
      dem <- file.path(out_dir_DEM, basename(unlist(res)))
      for (f in dem[file.exists(dem)]) .format_odm_raster(f, target, resol_ref, type = "dem")
   }

   invisible(unlist(res))
}


# Converts an ODM raster to the format of the Metashape outputs of canObsR
#  - ortho: 3 bands (alpha removed), 255 where there is no data (Metashape white background), INT1U
#  - dem: 1 band, NA where there is no data
#  - both: crs, resolution res, grid aligned on multiples of res
.format_odm_raster <- function(f, crs, res, type = c("ortho", "dem")) {
   type <- match.arg(type)
   r <- terra::rast(f)
   if (type == "ortho") {
      if (terra::nlyr(r) >= 4) {
         r <- terra::mask(r[[1:3]], r[[4]], maskvalues = 0)
      } else {
         r <- r[[1:3]]
      }
   }
   # template: target crs, grid aligned on multiples of res
   e <- terra::ext(terra::project(terra::as.polygons(terra::ext(r), crs = terra::crs(r)), crs))
   e <- terra::ext(floor(e$xmin / res) * res, ceiling(e$xmax / res) * res,
                   floor(e$ymin / res) * res, ceiling(e$ymax / res) * res)
   tmpl <- terra::rast(e, resolution = res, crs = crs)
   tmp <- paste0(tools::file_path_sans_ext(f), "_canobs_tmp.tif")
   gdal <- c("COMPRESS=LZW", "BIGTIFF=IF_SAFER", "TILED=YES")
   if (type == "ortho") {
      out <- terra::project(r, tmpl, method = "bilinear")
      out <- terra::classify(round(out), cbind(NA, 255))
      terra::writeRaster(out, tmp, datatype = "INT1U", gdal = gdal, overwrite = TRUE, NAflag = NA)
   } else {
      out <- terra::project(r, tmpl, method = "bilinear")
      terra::writeRaster(out, tmp, datatype = "FLT4S", gdal = gdal, overwrite = TRUE)
   }
   rm(r, out)
   gc()
   file.remove(f)
   file.rename(tmp, f)
   invisible(f)
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
   py <- new.env()
   reticulate::source_python(system.file("PYTHON/timesift_odm.py", package = "canObsR"), envir = py)
   res <- py$verify_projects(normalizePath(out_dir_project, winslash = "/"), verbose = verbose)
   data.frame(epoch = vapply(res, function(x) x$epoch, ""),
              ok = vapply(res, function(x) isTRUE(x$ok), TRUE),
              files_checked = vapply(res, function(x) length(x$checked), 0L),
              critical = vapply(res, function(x) paste(names(x$critical), collapse = "; "), ""),
              other = vapply(res, function(x) paste(names(x$other), collapse = "; "), ""))
}
