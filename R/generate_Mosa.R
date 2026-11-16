#' Generate orthomosaics
#'
#' @description
#' Implementation in R of a pipeline that performs the Time-SIFT process to time series of drone images. All photos are loaded and aligned in a single block, then orthomosaics and (optionally) DEMs will be generated for each date or flight.
#' Two engines are available: OpenDroneMap (`engine = "odm"`, free and open source, default) and the Metashape python API (`engine = "metashape"`, requires a license).
#'
#' @details
#' With `engine = "odm"`, the arguments `data_type`, `calibrate_col`, `sun_sensor` and `from_mesh` are not used (RGB only for now).
#' ODM always writes its products in the UTM zone of the images: the outputs are reprojected to `crs` when it is a different one.
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
#' @param calibrate_col logical. Applies white balance.
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
                       odm_args = NULL, overwrite = FALSE) {

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
                                  overwrite = overwrite
                                  )

   # ODM writes in the UTM zone of the images: reprojection if another crs is requested
   files <- unlist(res)
   if (!is.null(out_dir_DEM)) {
      if (out_dir_DEM == "") out_dir_DEM <- file.path(dirname(out_dir_ortho), "DEM")
      dem <- file.path(out_dir_DEM, basename(files))
      files <- c(files, dem[file.exists(dem)])
   }
   target <- gsub("::", ":", crs, fixed = TRUE)
   for (f in files) {
      r <- terra::rast(f)
      if (!terra::same.crs(r, target)) {
         message("Reprojecting ", basename(f), " to ", target)
         tmp <- paste0(tools::file_path_sans_ext(f), "_tmp.tif")
         terra::project(r, target, res = resol_ref, filename = tmp, overwrite = TRUE,
                        gdal = c("COMPRESS=LZW", "BIGTIFF=IF_SAFER"))
         rm(r)
         file.remove(f)
         file.rename(tmp, f)
      }
   }

   invisible(unlist(res))
}
