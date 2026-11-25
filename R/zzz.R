# Reminders when canobsR is attached (library(canobsR)): only what blocks the use of the package. Reads the
# configuration saved by canobs_setup() (no Python call: instantaneous). suppressPackageStartupMessages() hides them.
.onAttach <- function(libname, pkgname) {
   cfg <- tryCatch(canobs_config(), error = function(e) NULL)
   msg <- character(0)
   if (is.null(cfg)) {
      msg <- "The Python environment of canobsR is not installed yet: run canobsR::canobs_setup() once."
   } else {
      if (is.null(cfg$odm_path) || !file.exists(cfg$odm_path))
         msg <- c(msg, paste0("ODM (default photogrammetry engine) not found: ",
                              if (.Platform$OS.type == "windows") "canobsR::canobs_install_odm()" else "use odm_runner = \"docker\""))
      if (!is.null(cfg$versions$metashape) && !isTRUE(cfg$versions$metashape_activated))
         msg <- c(msg, paste0("Metashape module installed but no activated licence (engine = \"metashape\" unusable): ",
                              "canobsR::canobs_activate_metashape(\"<licence key>\")"))
   }
   if (length(msg)) packageStartupMessage(paste0("canobsR: ", msg, collapse = "\n"),
                                          "\n(diagnosis: canobsR::canobs_sitrep())")
}
