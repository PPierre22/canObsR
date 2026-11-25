test_that("environment variables of the child process point to the conda environment", {
   v <- canObsR:::.env_vars(tempdir())
   expect_true(all(c("PATH", "CONDA_PREFIX", "GDAL_DATA", "PROJ_DATA", "PROJ_LIB") %in% names(v)))
   expect_true(startsWith(v[["PATH"]], normalizePath(if (.Platform$OS.type == "windows") tempdir() else file.path(tempdir(), "bin"),
                                                     winslash = "\\", mustWork = FALSE)))
})

test_that("paths with spaces or accents are detected", {
   expect_true(canObsR:::.ascii_no_space("C:/canobs-miniforge3"))
   expect_false(canObsR:::.ascii_no_space("C:/Users/Jean Dupont/miniforge3"))
   expect_false(canObsR:::.ascii_no_space("C:/Users/h\u00e9l\u00e8ne/miniforge3"))
})

test_that("subfolders with a date are selected", {
   d <- file.path(tempdir(), "flights"); unlink(d, recursive = TRUE)
   for (s in c("RGB_20250101", "RGB_20250115_Matrice4E", "notes", "_tmp")) dir.create(file.path(d, s), recursive = TRUE)
   expect_setequal(suppressMessages(canObsR:::.select_subfolders(d)), c("RGB_20250101", "RGB_20250115_Matrice4E"))
})

test_that("canobspy round trip (only where canobs_setup() was run)", {
   skip_if(is.null(canObsR::canobs_config()), "canobs_setup() not run")
   d <- file.path(tempdir(), "flights2"); dir.create(file.path(d, "RGB_20250101"), recursive = TRUE, showWarnings = FALSE)
   res <- canObsR::check_flights(d, verbose = FALSE)
   expect_named(res, c("folders", "images"))
})
