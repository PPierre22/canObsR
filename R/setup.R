# Installation and diagnosis of what canObsR needs outside R: conda, the Python environment (canobspy, GDAL,
# AROSICS), and the photogrammetry engines (ODM, Metashape). See vignette("installation", package = "canObsR").
#
# Design rules:
#  - Windows, macOS and Linux; with or without an existing conda; without administrator rights;
#  - never modifies an existing Python or conda environment: everything goes into a dedicated environment;
#  - conda-forge packages only (--override-channels): no Anaconda "defaults" channel (licence of Anaconda Inc.);
#  - every step prints [OK], [--] (optional component absent) or [!!] (problem) and what to do about it;
#    the whole output (including conda's) goes to a log file, to send with a bug report.

# packages of the environment; normally installed from the explicit list of the platform (inst/python/locks,
# made by make_locks.py): no solving, same versions for everybody. These specs are only used where no list exists.
.canobs_pkgs <- c("python=3.12", "arosics>=1.14", "gdal>=3.10", "numpy", "pillow", "pip", "setuptools")

.conda_platform <- function() {
   os <- Sys.info()[["sysname"]]; arm <- Sys.info()[["machine"]] %in% c("arm64", "aarch64")
   if (os == "Windows") "win-64" else if (os == "Darwin") (if (arm) "osx-arm64" else "osx-64")
   else if (os == "Linux" && !arm) "linux-64" else NA_character_
}

.lock_file <- function() {
   p <- .conda_platform()
   f <- if (is.na(p)) "" else system.file("python", "locks", paste0("canobs-", p, ".txt"), package = "canObsR")
   if (nzchar(f)) f else NULL
}

# ---- console + log ------------------------------------------------------------------------------------
.say <- function(status, ..., log = getOption("canObsR.setup_log")) {
   tag <- c(ok = "[OK]", no = "[--]", ko = "[!!]", info = "    ", step = "\n==>")[[status]]
   msg <- paste0(tag, " ", paste0(..., collapse = ""))
   message(msg)
   if (!is.null(log)) cat(format(Sys.time(), "%H:%M:%S"), msg, "\n", file = log, append = TRUE)
   invisible(msg)
}

# runs a program; its output is shown (filtered) and fully written in the log
.run <- function(cmd, args, echo = TRUE, env = NULL, log = getOption("canObsR.setup_log")) {
   if (!is.null(log)) cat("\n$", cmd, paste(args, collapse = " "), "\n", file = log, append = TRUE)
   cb <- function(line, proc) {
      if (!is.null(log)) cat(line, "\n", sep = "", file = log, append = TRUE)
      line <- trimws(sub(".*\r", "", line))          # progress bars: last state only
      # conda lists hundreds of packages: only its stages and the problems are shown (all is in the log)
      keep <- if (identical(echo, "conda")) grepl("^(Collecting|Solving|Downloading|Preparing|Verifying|Executing)|error|fail|conflict|unsatisf|denied|proxy|https?://", line, ignore.case = TRUE) &&
                 !grepl("conda-pypi|bit\\.ly|conda-forge/", line) else isTRUE(echo)
      if (keep && nzchar(line) && !grepl("^[#|%. ]*$|\\| *[0-9]+%", line)) message("      ", substr(line, 1, 110))
   }
   r <- tryCatch(processx::run(cmd, args, error_on_status = FALSE, env = if (is.null(env)) NULL else c("current", env),
                               stdout_line_callback = cb, stderr_line_callback = cb, windows_hide_window = TRUE),
                 error = function(e) list(status = 127L, stdout = "", stderr = conditionMessage(e)))
   if (r$status == 127L && !is.null(log)) cat(r$stderr, "\n", file = log, append = TRUE)
   r
}

.ascii_no_space <- function(p) !grepl("[^!-~]", p)

# ---- conda ------------------------------------------------------------------------------------------
# conda executable (conda.exe on Windows, not conda.bat: it can be run without a shell)
.conda_exe <- function(p) {
   if (is.null(p) || !nzchar(p) || !file.exists(p)) return(NULL)
   if (.is_windows() && grepl("\\.bat$", p, ignore.case = TRUE)) {
      exe <- file.path(dirname(dirname(p)), "Scripts", "conda.exe")
      if (!file.exists(exe)) return(NULL)
      p <- exe
   }
   normalizePath(p, winslash = "/")
}

.find_conda <- function() {
   home <- path.expand("~")
   roots <- c(file.path(dirname(.default_conda_dir()), basename(.default_conda_dir())),
              file.path(home, c("miniforge3", "mambaforge", "miniconda3", "anaconda3", "Miniforge3", "Miniconda3", "Anaconda3")))
   if (.is_windows()) {
      la <- Sys.getenv("LOCALAPPDATA"); pd <- Sys.getenv("ProgramData", "C:/ProgramData")
      roots <- c(roots, "C:/canobs-miniforge3",
                 file.path(c(la, pd, "C:", file.path(la, "Programs")),
                           rep(c("miniforge3", "miniconda3", "anaconda3", "Miniforge3", "Miniconda3", "Anaconda3"), each = 4)))
      cand <- c(Sys.getenv("CONDA_EXE"), file.path(roots, "Scripts", "conda.exe"), Sys.which("conda"))
   } else {
      roots <- c(roots, "/opt/miniforge3", "/opt/miniconda3", "/opt/anaconda3", "/opt/conda", "/usr/local/miniconda3")
      cand <- c(Sys.getenv("CONDA_EXE"), file.path(roots, "bin", "conda"), Sys.which("conda"))
   }
   for (p in unique(cand[nzchar(cand)])) {
      exe <- .conda_exe(p)
      if (!is.null(exe)) return(exe)
   }
   NULL
}

.default_conda_dir <- function() {
   if (.is_windows()) {
      d <- file.path(Sys.getenv("LOCALAPPDATA"), "canobs", "miniforge3")
      # conda packages fail to install under paths with spaces or accents (frequent in Windows user names)
      if (!.ascii_no_space(d) || !nzchar(Sys.getenv("LOCALAPPDATA"))) d <- "C:/canobs-miniforge3"
      gsub("\\\\", "/", d)
   } else path.expand("~/.local/share/canobs/miniforge3")
}

.install_miniforge <- function(path) {
   os <- Sys.info()[["sysname"]]; arch <- Sys.info()[["machine"]]
   arm <- arch %in% c("arm64", "aarch64")
   file <- switch(os,
      Windows = "Miniforge3-Windows-x86_64.exe",
      Darwin = paste0("Miniforge3-MacOSX-", if (arm) "arm64" else "x86_64", ".sh"),
      paste0("Miniforge3-Linux-", if (arm) "aarch64" else "x86_64", ".sh"))
   url <- paste0("https://github.com/conda-forge/miniforge/releases/latest/download/", file)
   inst <- file.path(tempdir(), file)
   .say("info", "download of ", url, " (~90 MB)")
   old <- options(timeout = max(1800, getOption("timeout"))); on.exit(options(old), add = TRUE)
   ok <- tryCatch(utils::download.file(url, inst, mode = "wb", quiet = TRUE) == 0, error = function(e) FALSE)
   if (!ok || !file.exists(inst) || file.size(inst) < 1e7) {
      .say("ko", "download of Miniforge failed (no internet access, proxy, firewall?)")
      .say("info", "Download it by hand from https://conda-forge.org/download/ , install it (\"Just Me\"), then run ",
           "canobs_setup(conda = \"<miniforge folder>/", if (os == "Windows") "Scripts/conda.exe" else "bin/conda", "\")")
      stop("Download of Miniforge failed.", call. = FALSE)
   }
   if (dir.exists(path) && length(list.files(path))) {
      stop("The folder ", path, " already exists and is not empty: remove it, or give another conda_dir.", call. = FALSE)
   }
   dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
   .say("info", "installation of Miniforge in ", path, " (1-5 min, no administrator rights needed)")
   if (os == "Windows") {
      .run(inst, c("/InstallationType=JustMe", "/RegisterPython=0", "/AddToPath=0", "/NoShortcuts=1",
                   "/S", paste0("/D=", gsub("/", "\\\\", path))), echo = FALSE)
   } else {
      .run("bash", c(inst, "-b", "-p", path), echo = FALSE)
   }
   conda <- .conda_exe(if (os == "Windows") file.path(path, "Scripts", "conda.exe") else file.path(path, "bin", "conda"))
   if (is.null(conda)) {
      .say("ko", "installation of Miniforge failed (see the log). An antivirus can block it: try again, or install ",
           "Miniforge by hand (https://conda-forge.org/download/).")
      stop("Installation of Miniforge failed.", call. = FALSE)
   }
   conda
}

.conda_version <- function(conda) {
   r <- .run(conda, "--version", echo = FALSE)
   v <- regmatches(r$stdout, regexpr("[0-9]+\\.[0-9]+(\\.[0-9]+)?", r$stdout))
   if (length(v)) v else NA_character_
}

# folder of the environment `envname` (NULL if it does not exist)
.conda_env_dir <- function(conda, envname) {
   r <- .run(conda, c("env", "list", "--json"), echo = FALSE)
   envs <- tryCatch(unlist(jsonlite::fromJSON(r$stdout)$envs), error = function(e) character(0))
   envs <- gsub("\\\\", "/", envs)
   hit <- envs[basename(envs) == envname]
   if (length(hit)) hit[1] else NULL
}

# ---- engines ----------------------------------------------------------------------------------------
.find_odm <- function(odm_path = NULL) {
   cand <- c(odm_path, getOption("canObsR.odm_path"), Sys.getenv("ODM_PATH"),
             if (.is_windows()) "C:/ODM/run.bat")
   cand <- cand[!is.null(cand) & nzchar(cand)]
   hit <- cand[file.exists(cand) & !dir.exists(cand)]
   if (length(hit)) normalizePath(hit[1], winslash = "/") else NULL
}

.odm_version <- function(odm) {
   v <- file.path(dirname(odm), "VERSION")
   if (file.exists(v)) trimws(readLines(v, warn = FALSE)[1]) else "version unknown"
}

#' Install, or check, everything canObsR needs outside R
#'
#' @description
#' Run it once after installing canObsR, then after each update of canObsR (it updates canobspy, the Python part
#' of canObsR) or of ODM. It can be run again at any time: what is already installed is only checked.
#'
#' Steps, each reported as `[OK]`, `[--]` (optional component absent) or `[!!]` (problem, with what to do):
#' 1. **conda**: the one given in `conda`, else one already installed (Anaconda, Miniconda, Miniforge), else
#'    Miniforge is downloaded and installed in a folder of the user (no administrator rights needed);
#' 2. **Python environment** `envname` with Python, GDAL and AROSICS from conda-forge only (5 to 20 min the first
#'    time; an existing environment of another name is never modified);
#' 3. **canobspy**, the Python part of canObsR, shipped with the R package (same version);
#' 4. **checks**: imports and versions, and AROSICS co-registers two synthetic images with a known shift;
#' 5. **photogrammetry engines** (optional): ODM (and whether it can use an NVIDIA GPU), Metashape;
#' 6. the configuration is saved: the next R sessions use it automatically.
#'
#' The full output, including conda's, is written to a log file (path printed at the start and in
#' [canobs_sitrep()]): send it with any installation problem.
#'
#' @param envname character. Name of the conda environment of canObsR.
#' @param conda character. `"auto"`: the conda used before, else one found on this computer, else Miniforge is
#'   installed. `"miniforge"`: always use a Miniforge installed for canObsR (in `conda_dir`), whatever else is
#'   installed: the most predictable choice if the existing conda is old or misconfigured. Or the path of a conda
#'   executable (`.../Scripts/conda.exe` on Windows, `.../bin/conda` elsewhere).
#' @param conda_dir character. Where to install Miniforge. Default: in the user folder
#'   (`%LOCALAPPDATA%/canobs/miniforge3` on Windows, `~/.local/share/canobs/miniforge3` elsewhere), or
#'   `C:/canobs-miniforge3` if the Windows user folder contains spaces or accents.
#' @param odm_path character. Path of ODM (`run.bat` of the Windows installer of ODM, `run.sh` of a native Linux
#'   installation). Default: option `canObsR.odm_path`, environment variable `ODM_PATH`, `C:/ODM/run.bat`. ODM is
#'   only needed by [generate_Mosa()] with `engine = "odm"`.
#' @param metashape_wheel character. Path of the Metashape Python module (`Metashape-*.whl`, downloaded from
#'   https://www.agisoft.com/downloads/installer/) to install in the environment. Only needed by [generate_Mosa()]
#'   with `engine = "metashape"` (Metashape Professional licence).
#' @param reinstall logical. Removes and recreates the Python environment.
#' @param ask logical. Asks before installing Miniforge (default: in interactive sessions).
#'
#' @return Invisibly, the configuration (see [canobs_config()]).
#' @export
#'
#' @examples
#' \dontrun{
#' canObsR::canobs_setup()                                  # first installation, or check
#' canObsR::canobs_setup(odm_path = "D:/ODM/run.bat")        # ODM installed elsewhere than C:/ODM
#' canObsR::canobs_setup(conda = "miniforge")               # ignore the conda already installed
#' canObsR::canobs_sitrep()                                 # diagnosis, to send with a bug report
#' }
canobs_setup <- function(envname = "canobs", conda = "auto", conda_dir = NULL, odm_path = NULL,
                         metashape_wheel = NULL, reinstall = FALSE, ask = interactive()) {
   dir.create(.canobs_config_dir(), recursive = TRUE, showWarnings = FALSE)
   log <- file.path(.canobs_config_dir(), paste0("setup_", format(Sys.time(), "%Y%m%d_%H%M%S"), ".log"))
   old <- options(canObsR.setup_log = log); on.exit(options(old), add = TRUE)
   t0 <- Sys.time()
   prev <- canobs_config()
   if (is.null(conda_dir)) conda_dir <- .default_conda_dir()
   .say("info", "canObsR ", as.character(utils::packageVersion("canObsR")), " - installation of the Python environment")
   .say("info", R.version.string, ", ", utils::sessionInfo()$running, ", ", Sys.info()[["machine"]])
   .say("info", "log: ", log)

   # 1. conda --------------------------------------------------------------------------------------------
   .say("step", "1/6 conda")
   cbin <- NULL
   if (identical(conda, "miniforge")) {
      cbin <- .conda_exe(file.path(conda_dir, if (.is_windows()) "Scripts/conda.exe" else "bin/conda"))
   } else if (!identical(conda, "auto")) {
      cbin <- .conda_exe(conda)
      if (is.null(cbin)) stop("conda not found at ", conda, call. = FALSE)
   } else {
      cbin <- if (!is.null(prev$conda)) .conda_exe(prev$conda)
      if (is.null(cbin)) cbin <- .find_conda()
   }
   if (is.null(cbin)) {
      .say("no", if (identical(conda, "miniforge")) "no Miniforge for canObsR yet" else "no conda found on this computer")
      if (ask && !isTRUE(utils::askYesNo(paste0("Install Miniforge (conda of conda-forge, ~400 MB on disk) in ", conda_dir, "?"))))
         stop("Installation cancelled. canobs_setup(conda_dir = ...) installs Miniforge elsewhere; ",
              "canobs_setup(conda = ...) uses a conda of your choice.", call. = FALSE)
      cbin <- .install_miniforge(conda_dir)
   }
   cver <- .conda_version(cbin)
   if (is.na(cver)) {
      .say("ko", "conda does not answer: ", cbin, " (see the log)")
      .say("info", "Try canobs_setup(conda = \"miniforge\") to install a separate Miniforge for canObsR.")
      stop("conda does not work.", call. = FALSE)
   }
   .say("ok", "conda ", cver, ": ", cbin)
   if (!.ascii_no_space(cbin))
      .say("ko", "the path of conda contains spaces or accents: some packages fail to install there. If step 2 fails, ",
           "run canobs_setup(conda = \"miniforge\").")
   solver <- if (numeric_version(cver) < "23.10") "--solver=libmamba"
   if (length(solver)) .say("info", "conda < 23.10: the fast solver (libmamba) is requested; updating conda is advised")

   # 2. environment --------------------------------------------------------------------------------------
   .say("step", "2/6 Python environment '", envname, "' (Python, GDAL, AROSICS; conda-forge packages only)")
   envdir <- .conda_env_dir(cbin, envname)
   if (!is.null(envdir) && reinstall) {
      .say("info", "removal of the existing environment (reinstall = TRUE)")
      .run(cbin, c("env", "remove", "-y", "-n", envname), echo = FALSE)
      if (dir.exists(envdir)) unlink(envdir, recursive = TRUE)
      envdir <- NULL
   }
   if (is.null(envdir)) {
      lock <- .lock_file()
      r <- list(status = 1L)
      if (!is.null(lock)) {
         .say("info", "creation from the list of packages tested for ", .conda_platform(), " (",
              sub("^# ", "", readLines(lock, n = 3)[3]), ")")
         .say("info", "download and installation: 3 to 15 min depending on the connection (~0.5 GB, once)...")
         r <- .run(cbin, c("create", "-y", "-n", envname, "--file", lock), echo = "conda")
         if (r$status != 0) {
            .say("info", "the tested list could not be installed; second try, with conda resolving the versions")
            .run(cbin, c("env", "remove", "-y", "-n", envname), echo = FALSE)
         }
      }
      if (r$status != 0) {
         .say("info", "conda resolves the versions (can take 5-20 min and 2-3 GB of memory), then downloads ~0.5 GB...")
         args <- c("create", "-y", "-n", envname, "--override-channels", "-c", "conda-forge", solver, .canobs_pkgs)
         r <- .run(cbin, args, echo = "conda")
         if (r$status != 0 && length(solver) && grepl("solver", paste(r$stdout, r$stderr), ignore.case = TRUE)) {
            .say("info", "libmamba solver not available: classic solver (slower)")
            r <- .run(cbin, setdiff(args, solver), echo = "conda")
         }
      }
      envdir <- .conda_env_dir(cbin, envname)
      if (r$status != 0 || is.null(envdir)) {
         .say("ko", "conda could not create the environment (last lines above, full output in the log).")
         .say("info", "Frequent causes: no internet / proxy (conda needs https://conda.anaconda.org), antivirus, ",
              "disk full, path with spaces. canobs_setup(conda = \"miniforge\") avoids problems of an existing conda.")
         stop("Creation of the Python environment failed. Log: ", log, call. = FALSE)
      }
      .say("ok", "environment created")
   } else .say("ok", "environment already there (reinstall = TRUE recreates it)")
   py <- .env_python_of(envdir)
   if (is.null(py)) stop("No Python in the environment ", envdir, ": run canobs_setup(reinstall = TRUE).", call. = FALSE)
   .say("ok", "Python of the environment: ", py)
   pyrun <- function(args, echo = FALSE) .run(py, args, echo = echo, env = .env_vars(envdir))

   # 3. canobspy -----------------------------------------------------------------------------------------
   .say("step", "3/6 canobspy (Python part of canObsR)")
   src <- normalizePath(system.file("python", package = "canObsR"), winslash = "/")
   build <- file.path(tempdir(), "canobspy_src")          # pip writes in the source folder: work on a copy
   unlink(build, recursive = TRUE); dir.create(build)
   file.copy(list.files(src, full.names = TRUE), build, recursive = TRUE)
   r <- pyrun(c("-m", "pip", "install", "--no-deps", "--no-build-isolation", "--upgrade", "--force-reinstall",
                "--disable-pip-version-check", "--no-input", build))
   if (r$status != 0) {
      .say("ko", "pip could not install canobspy (see the log)")
      stop("Installation of canobspy failed. Log: ", log, call. = FALSE)
   }
   if (!is.null(metashape_wheel)) {
      if (!file.exists(metashape_wheel)) stop("File not found: ", metashape_wheel, call. = FALSE)
      .say("info", "installation of the Metashape module: ", basename(metashape_wheel))
      r <- pyrun(c("-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "--no-input",
                   normalizePath(metashape_wheel, winslash = "/")))
      if (r$status != 0) .say("ko", "pip could not install ", basename(metashape_wheel), " (see the log)")
   }

   # 4. checks -------------------------------------------------------------------------------------------
   .say("step", "4/6 checks")
   r <- pyrun(c("-m", "canobspy", "info"))
   v <- tryCatch(jsonlite::fromJSON(utils::tail(strsplit(r$stdout, "\n")[[1]], 1)), error = function(e) NULL)
   if (is.null(v)) {
      .say("ko", "canobspy cannot be imported:"); message(r$stderr)
      stop("The Python environment does not work: canobs_setup(reinstall = TRUE). Log: ", log, call. = FALSE)
   }
   bad <- FALSE
   for (k in c("python", "canobspy", "numpy", "gdal", "arosics")) {
      if (is.null(v[[k]])) { .say("ko", k, ": ", v[[paste0(k, "_error")]]); bad <- TRUE } else .say("ok", k, " ", v[[k]])
   }
   if (bad) stop("Packages missing in the environment: canobs_setup(reinstall = TRUE). Log: ", log, call. = FALSE)
   .say("info", "AROSICS test: co-registration of two synthetic images with a known shift...")
   r <- pyrun(c("-m", "canobspy", "selftest"))
   st <- tryCatch(jsonlite::fromJSON(utils::tail(strsplit(r$stdout, "\n")[[1]], 1)), error = function(e) NULL)
   if (isTRUE(st$ok)) {
      .say("ok", "AROSICS: image shifted by (", paste(st$expected_px, collapse = ", "), ") px, correction found (",
           paste(st$found_px, collapse = ", "), ") px, ", st$local_points, " local tie points, ", st$seconds, " s")
   } else {
      .say("ko", "AROSICS test failed: ", if (!is.null(st$error)) st$error else paste(utils::tail(strsplit(r$stderr, "\n")[[1]], 3), collapse = " "))
      .say("info", "The installation can still be used for the mosaics; send the log to the developers.")
   }

   # 5. engines ------------------------------------------------------------------------------------------
   .say("step", "5/6 photogrammetry engines (optional)")
   odm <- .find_odm(if (!is.null(odm_path)) odm_path else prev$odm_path)
   if (!is.null(odm_path) && is.null(odm)) .say("ko", "ODM not found at ", odm_path)
   if (is.null(odm) && .is_windows() && ask &&
       isTRUE(utils::askYesNo(paste0("ODM (default photogrammetry engine of canObsR) is not installed. Install ODM ",
                                     .odm_tested, " in C:/ODM now (download of 245 MB, 5-10 min)?")))) {
      odm <- tryCatch(.install_odm("C:/ODM", .odm_tested), error = function(e) { .say("ko", conditionMessage(e)); NULL })
   }
   gpu <- if (is.null(odm)) .odm_missing_message() else .odm_report(odm, py, envdir)
   if (!is.null(v$metashape)) .say("ok", "Metashape module ", v$metashape, " (the licence is checked when it is used)")
   else .say("no", "Metashape module not installed: needed by generate_Mosa(engine = \"metashape\") only ",
             "(canobs_setup(metashape_wheel = \"<path>/Metashape-...whl\"))")

   # 6. configuration ------------------------------------------------------------------------------------
   .say("step", "6/6 configuration")
   cfg <- list(conda = cbin, envname = envname, envdir = envdir, python = py, odm_path = odm, versions = v,
               selftest = st, canObsR = as.character(utils::packageVersion("canObsR")), date = format(Sys.time()),
               log = log)
   saveRDS(cfg, .canobs_config_file())
   .say("ok", "saved in ", .canobs_config_file())

   # summary: what is left to do (the messages of step 5 are easy to miss in a long output)
   todo <- character(0)
   if (!isTRUE(st$ok)) todo <- c(todo, "AROSICS test failed (step 4): send the log to the developers")
   if (is.null(odm)) {
      todo <- c(todo, if (.is_windows()) "install ODM, needed to make mosaics:   canObsR::canobs_install_odm()"
                      else "ODM, needed to make mosaics, runs with docker: generate_Mosa(..., odm_runner = \"docker\")")
   } else if (identical(gpu$status, "dummy_present")) {
      todo <- c(todo, paste0("ODM will not use the GPU: rename ", gpu$path, " to nvcuda.dll.odm_dummy (as administrator)"))
   }
   if (is.null(v$metashape)) todo <- c(todo, paste0("optional, only for engine = \"metashape\": ",
                                                    "canObsR::canobs_setup(metashape_wheel = \"<path>/Metashape-...whl\")"))
   bar <- strrep("-", 100)
   message("\n", bar)
   message(" canObsR: Python environment ready in ", round(as.numeric(difftime(Sys.time(), t0, units = "mins")), 1), " min.")
   if (length(todo)) {
      message(" Still to do:")
      for (k in seq_along(todo)) message("   ", k, ". ", todo[k])
   } else message(" Nothing left to do: canObsR is ready.")
   message(" Diagnosis at any time: canObsR::canobs_sitrep()     Log: ", log)
   message(bar)
   if (!is.null(log)) cat(" Still to do:", todo, sep = "\n", file = log, append = TRUE)
   invisible(cfg)
}

# ---- ODM ----------------------------------------------------------------------------------------------
.odm_tested <- "3.6.2"      # version of ODM used for the tests and defaults of canObsR (11/2026)

.odm_missing_message <- function() {
   .say("no", "ODM not found. It is the default photogrammetry engine: generate_Mosa() needs it to make mosaics.")
   if (.is_windows()) {
      .say("info", "To install it (no administrator rights needed):")
      .say("info", "   canObsR::canobs_install_odm()            # ODM ", .odm_tested, " in C:/ODM, found automatically afterwards")
      .say("info", "Already installed elsewhere?  canObsR::canobs_setup(odm_path = \"D:/ODM/run.bat\")")
   } else {
      .say("info", "On Linux and macOS, ODM runs with docker: generate_Mosa(..., odm_runner = \"docker\")",
           if (nzchar(Sys.which("docker"))) " (docker is installed)" else " (install docker first: https://docs.docker.com/get-docker/)")
      .say("info", "Native installation on Linux: canObsR::canobs_setup(odm_path = \"<ODM folder>/run.sh\")")
   }
   invisible(list(status = "no_odm"))
}

# version, then (Windows) whether OpenMVS can use the NVIDIA GPU; renames ODM's dummy nvcuda.dll if needed
.odm_report <- function(odm, py, envdir) {
   .say("ok", "ODM ", .odm_version(odm), ": ", odm)
   if (!.is_windows()) return(invisible(NULL))
   g <- tryCatch(.canobs_python(c("-c", sprintf(paste0(
           "import json; from canobspy.timesift_odm import check_odm_gpu; ",
           "print(json.dumps(check_odm_gpu(r'%s', fix=True)))"), odm)), python = py, envdir = envdir, echo = FALSE),
           error = function(e) NULL)
   g <- tryCatch(jsonlite::fromJSON(utils::tail(strsplit(g$stdout, "\n")[[1]], 1)), error = function(e) list(status = "error"))
   msg <- switch(g$status,
      renamed = c("ok", "NVIDIA GPU: ODM's dummy nvcuda.dll renamed, dense matching will use the GPU"),
      no_dummy = c("ok", "NVIDIA GPU: usable by ODM"),
      no_nvidia_driver = c("no", "no NVIDIA GPU: ODM will compute on the processor (dense matching 3-4 x slower, fine for tests)"),
      dummy_present = c("ko", paste0("ODM's dummy nvcuda.dll hides the NVIDIA driver and could not be renamed: rename ",
                                     g$path, " to nvcuda.dll.odm_dummy (administrator rights)")),
      c("ko", "GPU check of ODM failed (see the log)"))
   .say(msg[1], msg[2])
   invisible(g)
}

.install_odm <- function(dir, version) {
   dir <- gsub("\\\\", "/", dir)
   if (!.ascii_no_space(dir)) stop("ODM does not work in a folder with spaces or accents: choose another dir.", call. = FALSE)
   run <- file.path(dir, "run.bat")
   url <- sprintf("https://github.com/OpenDroneMap/ODM/releases/download/v%s/ODM_Setup_%s.exe", version, version)
   inst <- file.path(tempdir(), basename(url))
   .say("info", "download of ", url, " (245 MB)...")
   old <- options(timeout = max(3600, getOption("timeout"))); on.exit(options(old), add = TRUE)
   ok <- tryCatch(utils::download.file(url, inst, mode = "wb", quiet = TRUE) == 0, error = function(e) FALSE)
   if (!ok || !file.exists(inst) || file.size(inst) < 1e8)
      stop("Download of ODM failed (internet, proxy?). Download ", url, " by hand, install it in ", dir,
           ", then run canObsR::canobs_setup().", call. = FALSE)
   .say("info", "installation of ODM ", version, " in ", dir, " (5-10 min). If Windows asks for permission to install ",
        "Microsoft Visual C++ libraries, accept.")
   ilog <- file.path(.canobs_config_dir(), paste0("odm_install_", format(Sys.time(), "%Y%m%d_%H%M%S"), ".log"))
   r <- .run(inst, c("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/CURRENTUSER",
                     paste0("/DIR=", gsub("/", "\\\\", dir)), paste0("/LOG=", gsub("/", "\\\\", ilog))), echo = FALSE)
   unlink(inst)
   if (!file.exists(run))
      stop("Installation of ODM failed (exit status ", r$status, "; installer log: ", ilog, "). An antivirus can block ",
           "it: install ", url, " by hand in ", dir, ", then run canObsR::canobs_setup().", call. = FALSE)
   .say("ok", "ODM installed in ", dir)
   normalizePath(run, winslash = "/")
}

#' Install OpenDroneMap (Windows)
#'
#' @description
#' Downloads the Windows installer of [OpenDroneMap](https://opendronemap.org) (ODM), the default photogrammetry
#' engine of canObsR, installs it without administrator rights, records it in the configuration of canObsR and
#' checks whether it can use the NVIDIA GPU (see [canobs_setup()]). On Linux and macOS, ODM runs with docker
#' (`generate_Mosa(..., odm_runner = "docker")`): this function only says so.
#'
#' @param dir character. Installation folder (no spaces or accents). Default `C:/ODM`, the folder of the installer.
#' @param version character. Version of ODM. Default: the version used to test and set the defaults of canObsR.
#'   `"latest"` for the latest release.
#' @param reinstall logical. Installs even if ODM is already in `dir`.
#' @return Invisibly, the path of `run.bat`.
#' @export
#' @examples
#' \dontrun{
#' canObsR::canobs_install_odm()
#' }
canobs_install_odm <- function(dir = "C:/ODM", version = .odm_tested, reinstall = FALSE) {
   if (!.is_windows()) {
      .odm_missing_message()
      return(invisible(NULL))
   }
   cfg <- canobs_config()
   if (is.null(cfg)) stop("Run canObsR::canobs_setup() first.", call. = FALSE)
   old <- options(canObsR.setup_log = cfg$log); on.exit(options(old), add = TRUE)
   if (identical(version, "latest")) {
      rel <- tryCatch(jsonlite::fromJSON("https://api.github.com/repos/OpenDroneMap/ODM/releases/latest")$tag_name,
                      error = function(e) NULL)
      version <- if (is.null(rel)) .odm_tested else sub("^v", "", rel)
   }
   run <- file.path(dir, "run.bat")
   if (file.exists(run) && !reinstall) {
      .say("ok", "ODM ", .odm_version(run), " already installed in ", dir, " (reinstall = TRUE to install it again)")
   } else run <- .install_odm(dir, version)
   run <- normalizePath(run, winslash = "/")
   .odm_report(run, cfg$python, cfg$envdir)
   cfg$odm_path <- run
   saveRDS(cfg, .canobs_config_file())
   .say("ok", "ODM recorded in the configuration of canObsR: generate_Mosa() will find it.")
   invisible(run)
}

#' Diagnosis of the installation of canObsR
#'
#' Prints what [canobs_setup()] installed and checks that it still works, without installing anything. Copy its
#' output (and the log file it names) in any bug report.
#' @return Invisibly, the configuration (see [canobs_config()]).
#' @export
canobs_sitrep <- function() {
   message("canObsR ", as.character(utils::packageVersion("canObsR")), " | ", R.version.string, " | ",
           utils::sessionInfo()$running, " | ", Sys.info()[["machine"]])
   cfg <- canobs_config()
   if (is.null(cfg)) {
      .say("ko", "canobs_setup() has not been run on this computer (or its configuration was deleted).")
      return(invisible(NULL))
   }
   .say("info", "installed on ", cfg$date, " with canObsR ", cfg$canObsR, "; log: ", cfg$log)
   .say(if (file.exists(cfg$conda)) "ok" else "ko", "conda: ", cfg$conda)
   ok_py <- !is.null(cfg$python) && file.exists(cfg$python)
   .say(if (ok_py) "ok" else "ko", "Python environment '", cfg$envname, "': ", cfg$python)
   if (ok_py) {
      r <- tryCatch(.canobs_python(c("-m", "canobspy", "info"), echo = FALSE), error = function(e) NULL)
      v <- tryCatch(jsonlite::fromJSON(utils::tail(strsplit(r$stdout, "\n")[[1]], 1)), error = function(e) NULL)
      if (is.null(v)) .say("ko", "canobspy cannot be imported: run canobs_setup()") else {
         .say("ok", "canobspy ", v$canobspy, ", Python ", v$python, ", GDAL ", v$gdal, ", AROSICS ", v$arosics,
              if (!is.null(v$metashape)) paste0(", Metashape ", v$metashape))
         if (!identical(v$canobspy, as.character(utils::packageVersion("canObsR"))))
            .say("ko", "canobspy ", v$canobspy, " differs from canObsR ", as.character(utils::packageVersion("canObsR")),
                 ": run canobs_setup() to update it")
      }
   }
   odm <- .find_odm(cfg$odm_path)
   .say(if (is.null(odm)) "no" else "ok", "ODM: ", if (is.null(odm)) "not found" else paste(.odm_version(odm), odm))
   if (is.null(odm)) .say("info", if (.is_windows()) "   canObsR::canobs_install_odm() installs it" else
                         "   with docker: generate_Mosa(..., odm_runner = \"docker\")")
   smi <- Sys.which("nvidia-smi")
   gpu <- if (nzchar(smi)) tryCatch(system2(smi, c("--query-gpu=name,driver_version", "--format=csv,noheader"),
                                            stdout = TRUE, stderr = FALSE), error = function(e) character(0))
   .say(if (length(gpu)) "ok" else "no", "NVIDIA GPU: ", if (length(gpu)) paste(gpu, collapse = "; ") else "none")
   invisible(cfg)
}
