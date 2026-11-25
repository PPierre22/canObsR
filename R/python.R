# How canObsR runs its Python code (canobspy).
#
# Python always runs in a separate process (python -m canobspy ...), with the conda environment created by
# canobs_setup(), never inside the R session (no reticulate):
#  - R (terra, sf) and Python (GDAL of conda-forge) load different versions of GDAL and PROJ: in the same process
#    they can conflict (crash, "proj.db" errors). R packages also set PROJ_LIB / GDAL_DATA for their own GDAL; the
#    child process gets the values of the conda environment instead;
#  - a failure of Python leaves R alive, and Esc / Ctrl+C in R stops the Python process;
#  - the R session is never bound to a Python: no "restart R" problems with other packages that use Python.

.canobs_config_dir <- function() gsub("\\\\", "/", tools::R_user_dir("canObsR", which = "config"))
.canobs_config_file <- function() file.path(.canobs_config_dir(), "config.rds")

#' Configuration saved by canobs_setup()
#'
#' @return A list (`conda`, `envname`, `envdir`, `python`, `odm_path`, `versions`, ...), or NULL if
#'   [canobs_setup()] has never been run on this computer.
#' @export
canobs_config <- function() {
   f <- .canobs_config_file()
   if (file.exists(f)) readRDS(f) else NULL
}

.is_windows <- function() .Platform$OS.type == "windows"

# python executable of a conda environment folder
.env_python_of <- function(envdir) {
   p <- if (.is_windows()) file.path(envdir, "python.exe") else file.path(envdir, "bin", "python")
   if (file.exists(p)) normalizePath(p, winslash = "/") else NULL
}

# Environment variables of an activated conda environment (what "conda activate" does), for the child process
.env_vars <- function(envdir) {
   envdir <- normalizePath(envdir, winslash = "/", mustWork = FALSE)
   if (.is_windows()) {
      bins <- file.path(envdir, c("", "Library/mingw-w64/bin", "Library/usr/bin", "Library/bin", "Scripts", "bin"))
      share <- file.path(envdir, "Library", "share")
   } else {
      bins <- file.path(envdir, "bin")
      share <- file.path(envdir, "share")
   }
   path <- paste(c(normalizePath(bins, winslash = "\\", mustWork = FALSE), Sys.getenv("PATH")),
                 collapse = .Platform$path.sep)
   v <- c(PATH = path, CONDA_PREFIX = envdir, PYTHONNOUSERSITE = "1", PYTHONIOENCODING = "utf-8",
          PYTHONUNBUFFERED = "1", PYTHONHOME = "", PYTHONPATH = "")
   for (x in list(c("GDAL_DATA", "gdal"), c("PROJ_DATA", "proj"), c("PROJ_LIB", "proj"))) {
      d <- file.path(share, x[2])
      v[x[1]] <- if (dir.exists(d)) d else ""
   }
   v["GDAL_DRIVER_PATH"] <- ""
   v
}

# Runs python of the environment with args; output shown live and appended to `log`; returns processx::run() result
.canobs_python <- function(args, python = NULL, envdir = NULL, echo = TRUE, log = NULL, error_on_status = FALSE,
                           timeout = Inf) {
   if (is.null(python)) {
      cfg <- canobs_config()
      if (is.null(cfg) || is.null(cfg$python) || !file.exists(cfg$python))
         stop("The Python environment of canObsR is not installed (or has moved): run canObsR::canobs_setup() once.",
              call. = FALSE)
      python <- cfg$python; envdir <- cfg$envdir
   }
   if (is.null(envdir)) envdir <- if (.is_windows()) dirname(python) else dirname(dirname(python))
   cb <- function(line, proc) {
      if (echo) cat(line, "\n", sep = "")
      if (!is.null(log)) cat(line, "\n", sep = "", file = log, append = TRUE)
   }
   processx::run(python, c("-u", args), env = c("current", .env_vars(envdir)), error_on_status = error_on_status,
                 stdout_line_callback = cb, stderr_line_callback = cb, timeout = timeout,
                 windows_hide_window = TRUE, cleanup_tree = TRUE)
}

# Calls canobspy.<module>.<fun>(...) in the Python environment of canObsR and returns its result (from JSON).
# Arguments are sent as JSON: NULL -> None, length-1 vectors -> scalars, use I() or as.list() for lists.
.canobs_call <- function(module, fun, ..., .echo = TRUE) {
   args <- list(...)
   args <- args[!vapply(args, is.null, TRUE)]
   fa <- tempfile("canobs_args_", fileext = ".json"); fr <- tempfile("canobs_res_", fileext = ".json")
   on.exit(unlink(c(fa, fr)), add = TRUE)
   jsonlite::write_json(args, fa, auto_unbox = TRUE, null = "null", na = "null", digits = NA)
   res <- .canobs_python(c("-m", "canobspy", "call", module, fun, fa, fr), echo = .echo)
   if (!file.exists(fr)) {
      stop("Python stopped before the end of canobspy.", module, ".", fun, "() (exit status ", res$status,
           "). See the messages above.", call. = FALSE)
   }
   out <- jsonlite::read_json(fr, simplifyVector = FALSE)
   if (!isTRUE(out$ok)) stop("canobspy.", module, ".", fun, "(): ", out$error, call. = FALSE)
   out$result
}
