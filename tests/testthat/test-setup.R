test_that("environment variables of the child process point to the conda environment", {
   v <- canobsR:::.env_vars(tempdir())
   expect_true(all(c("PATH", "CONDA_PREFIX", "GDAL_DATA", "PROJ_DATA", "PROJ_LIB") %in% names(v)))
   expect_true(startsWith(v[["PATH"]], normalizePath(if (.Platform$OS.type == "windows") tempdir() else file.path(tempdir(), "bin"),
                                                     winslash = "\\", mustWork = FALSE)))
})

test_that("paths with spaces or accents are detected", {
   expect_true(canobsR:::.ascii_no_space("C:/canobs-miniforge3"))
   expect_false(canobsR:::.ascii_no_space("C:/Users/Jean Dupont/miniforge3"))
   expect_false(canobsR:::.ascii_no_space("C:/Users/h\u00e9l\u00e8ne/miniforge3"))
})

test_that("subfolders with a date are selected", {
   d <- file.path(tempdir(), "flights"); unlink(d, recursive = TRUE)
   for (s in c("RGB_20250101", "RGB_20250115_Matrice4E", "notes", "_tmp")) dir.create(file.path(d, s), recursive = TRUE)
   expect_setequal(suppressMessages(canobsR:::.select_subfolders(d)), c("RGB_20250101", "RGB_20250115_Matrice4E"))
})

test_that("canobspy round trip (only where canobs_setup() was run)", {
   skip_if(is.null(canobsR::canobs_config()), "canobs_setup() not run")
   d <- file.path(tempdir(), "flights2"); dir.create(file.path(d, "RGB_20250101"), recursive = TRUE, showWarnings = FALSE)
   res <- canobsR::check_flights(d, verbose = FALSE)
   expect_named(res, c("folders", "images"))
})

test_that("dates are split into chunks of nearly equal sizes", {
   x <- sprintf("2023%04d", 1:23)
   s <- canobsR:::.split_even(x, 6)
   expect_length(s, 4); expect_true(all(lengths(s) %in% 5:6)); expect_setequal(unlist(s), x)
   s <- canobsR:::.split_even(x, 6, consecutive = TRUE)
   expect_true(all(lengths(s) %in% 5:6)); expect_equal(unlist(s, use.names = FALSE), x)
})

test_that("unknown advanced options are refused", {
   expect_error(canobsR:::.check_options(list(chunksize = 5)), "Unknown option")
   expect_equal(canobsR:::.check_options(list(chunk_size = 5))$chunk_size, 5)
})

test_that("plan_mosaics never mixes drones by default", {
   skip_if(is.null(canobsR::canobs_config()), "canobs_setup() not run")
   d <- file.path(tempdir(), "site"); unlink(d, recursive = TRUE)
   for (f in c(sprintf("RGB_202301%02d", 1:9), sprintf("RGB_202302%02d_Matrice4E", 1:3))) {
      dir.create(file.path(d, f), recursive = TRUE); file.create(file.path(d, f, "a.JPG"))
   }
   p <- suppressMessages(canobsR::plan_mosaics(d, file.path(tempdir(), "site_out"), chunk_size = 4))
   expect_true(all(tapply(p$drone, p$chunk, function(x) length(unique(x))) == 1))
   expect_warning(suppressMessages(canobsR::plan_mosaics(d, file.path(tempdir(), "site_out2"), chunk_size = 4,
                                                         split_drones = FALSE)), "mix drones")
})
