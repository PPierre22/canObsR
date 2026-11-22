# =====================================================================================================
# ODM (native Windows): does the dense matching (OpenMVS DensifyPointCloud) use the GPU?
# =====================================================================================================
# Observed in benchmark2: GPU at 0 % and 173 MB of GPU memory during the whole ODM run, although ODM writes
# "CUDA drivers detected" and passes --cuda-device -1 (= best GPU) to OpenMVS. OpenMVS falls back to the CPU
# without any message (ODM runs it with -v 0).
#
# Hypothesis: the ODM installer puts a DUMMY nvcuda.dll next to DensifyPointCloud.exe
# (C:/ODM/SuperBuild/install/bin, so that OpenMVS starts on PCs without an NVIDIA card). Windows loads a DLL from the
# folder of the program BEFORE C:/Windows/System32: OpenMVS would get the dummy, not the real NVIDIA driver, and
# CUDA could never start.
#
# What this script does (about 15-30 min, nothing of the ODM projects is modified):
#   1. versions: GPU, driver, CUDA of the driver; the two nvcuda.dll (size, version);
#   2. test A: DensifyPointCloud as ODM runs it, on a date of benchmark2 at reduced resolution
#      (--resolution-level 2), in a separate working folder, with full messages (-v 2), GPU logged every second
#      and the DLLs loaded by the program listed while it runs;
#   3. if the dummy DLL is there: it is renamed (nvcuda.dll -> nvcuda.dll.odm_dummy) and test B is run;
#   4. if test B uses the GPU and finishes: the renaming is KEPT (all next ODM runs use the GPU);
#      otherwise the dummy DLL is put back. To undo by hand: rename nvcuda.dll.odm_dummy to nvcuda.dll.
# Results in <bench_dir>/gpu_test: gpu_test_summary.csv, logs of OpenMVS, GPU logs.
# =====================================================================================================

if (!exists("my_path")) my_path <- "D:/canObsR/data/canObsR_data/canObsR_data"
gpu_scene_dir <- file.path(my_path, "benchmark2", "project_odm", "20220427", "opensfm", "undistorted", "openmvs")
gpu_out_dir   <- file.path(my_path, "benchmark2", "gpu_test")
odm_bin       <- "C:/ODM/SuperBuild/install/bin"
gpu_timeout_min <- 45                       # a test running longer is stopped

dir.create(gpu_out_dir, showWarnings = FALSE, recursive = TRUE)
win <- function(p) normalizePath(p, winslash = "\\", mustWork = FALSE)
ps  <- function(cmd) suppressWarnings(system2("powershell", c("-NoProfile", "-Command", shQuote(cmd)), stdout = TRUE, stderr = TRUE))
say <- function(...) { m <- paste0(format(Sys.time(), "%H:%M:%S"), " [GPU test] ", ...); message(m)
                        cat(m, "\n", file = file.path(gpu_out_dir, "gpu_test_log.txt"), append = TRUE) }

# ---- 1. versions -----------------------------------------------------------------------------------
smi <- Sys.which("nvidia-smi")
if (smi == "") stop("nvidia-smi not found")
say("GPU: ", paste(system2(smi, c("--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"), stdout = TRUE), collapse = " | "))
say("CUDA of the driver: ", trimws(sub(".*CUDA Version: *([0-9.]+).*", "\\1", grep("CUDA Version", system2(smi, stdout = TRUE), value = TRUE)[1])))
dll_local <- file.path(odm_bin, "nvcuda.dll")
dll_sys   <- file.path(Sys.getenv("SystemRoot"), "System32", "nvcuda.dll")
dll_info <- function(f) if (file.exists(f)) paste0(round(file.size(f) / 1024), " KB, version ",
   paste(ps(sprintf("(Get-Item '%s').VersionInfo.FileVersion", win(f))), collapse = " ")) else "absent"
say("nvcuda.dll next to OpenMVS (", odm_bin, "): ", dll_info(dll_local))
say("nvcuda.dll of the NVIDIA driver (System32): ", dll_info(dll_sys))
say("CUDA libraries shipped with ODM: ", paste(basename(list.files(odm_bin, pattern = "^(cudart|cublas|cufft|curand|cusolver|cusparse).*\\.dll$", ignore.case = TRUE)), collapse = ", "))

densify <- file.path(odm_bin, "DensifyPointCloud.exe")
scene   <- file.path(gpu_scene_dir, "scene.mvs")
if (!file.exists(densify)) stop("Not found: ", densify)
if (!file.exists(scene)) stop("Not found: ", scene, " (ODM project of benchmark2)")

# ---- 2. one test run ---------------------------------------------------------------------------------
run_densify_test <- function(label) {
   wdir <- file.path(gpu_scene_dir, paste0("gputest_", label))      # same depth as ODM's 'depthmaps' folder
   unlink(wdir, recursive = TRUE); dir.create(wdir)
   gpu_log <- file.path(gpu_out_dir, paste0("gpu_usage_", label, ".csv"))
   unlink(gpu_log)
   system2(smi, c("--query-gpu=timestamp,utilization.gpu,memory.used", "--format=csv,noheader,nounits",
                  "-l", "1", "-f", shQuote(win(gpu_log))), wait = FALSE)
   args <- c(shQuote(win(scene)), "--resolution-level", "2",
             "--dense-config-file", shQuote(win(file.path(gpu_scene_dir, "Densify.ini"))),
             "--max-resolution", "5472", "--min-resolution", "640", "--number-views-fuse", "2",
             "--sub-resolution-levels", "2", "--archive-type", "3",
             "-w", shQuote(win(wdir)), "-o", shQuote(win(file.path(wdir, "scene_dense_test.mvs"))),
             "-v", "2", "--cuda-device", "-1")
   out_txt <- file.path(gpu_out_dir, paste0("densify_", label, "_console.txt"))
   t0 <- Sys.time()
   say("test ", label, ": DensifyPointCloud started")
   system2(densify, args, stdout = out_txt, stderr = out_txt, wait = FALSE)
   modules <- character(0)
   running <- function() any(grepl("DensifyPointCloud", system2("tasklist", c("/FI", shQuote("IMAGENAME eq DensifyPointCloud.exe")), stdout = TRUE)))
   repeat {
      Sys.sleep(10)
      if (length(modules) == 0) {     # DLLs loaded by the running program (the nvcuda.dll really used)
         modules <- ps("(Get-Process DensifyPointCloud -ErrorAction SilentlyContinue).Modules | Where-Object { $_.ModuleName -match '^(nvcuda|cudart|nvcuda_)' } | ForEach-Object { $_.FileName }")
      }
      if (!running()) break
      if (difftime(Sys.time(), t0, units = "mins") > gpu_timeout_min) {
         say("test ", label, ": longer than ", gpu_timeout_min, " min, stopped")
         system("taskkill /IM DensifyPointCloud.exe /F", ignore.stdout = TRUE, ignore.stderr = TRUE)
         break
      }
   }
   minutes <- round(as.numeric(difftime(Sys.time(), t0, units = "mins")), 1)
   system("taskkill /IM nvidia-smi.exe /F", ignore.stdout = TRUE, ignore.stderr = TRUE)
   g <- tryCatch(read.csv(gpu_log, header = FALSE, col.names = c("time", "util", "mem"), strip.white = TRUE),
                 error = function(e) data.frame(util = NA, mem = NA))
   ok <- file.exists(file.path(wdir, "scene_dense_test.mvs"))
   # OpenMVS log (written in the working folder) and console: lines about CUDA / GPU
   logs <- c(list.files(wdir, pattern = "\\.log$", full.names = TRUE), out_txt)
   for (f in list.files(wdir, pattern = "\\.log$", full.names = TRUE)) file.copy(f, file.path(gpu_out_dir, paste0(label, "_", basename(f))), overwrite = TRUE)
   cuda_lines <- unique(unlist(lapply(logs, function(f) grep("cuda|gpu|device", readLines(f, warn = FALSE), ignore.case = TRUE, value = TRUE))))
   res <- data.frame(test = label, finished = ok, minutes = minutes,
                     gpu_util_max = suppressWarnings(max(g$util, na.rm = TRUE)),
                     gpu_util_mean = round(mean(g$util, na.rm = TRUE), 1),
                     gpu_mem_max_MB = suppressWarnings(max(g$mem, na.rm = TRUE)),
                     nvcuda_loaded = if (length(modules)) paste(modules, collapse = " ; ") else "not seen",
                     cuda_messages = paste(head(trimws(cuda_lines), 5), collapse = " | "))
   say("test ", label, ": ", minutes, " min, finished = ", ok, ", GPU max ", res$gpu_util_max, " %, GPU memory max ",
       res$gpu_mem_max_MB, " MB, nvcuda.dll used: ", res$nvcuda_loaded)
   if (nzchar(res$cuda_messages)) say("test ", label, ": OpenMVS messages: ", res$cuda_messages)
   unlink(wdir, recursive = TRUE)                                   # the ODM project is left as it was
   res
}

gpu_results <- run_densify_test("A_as_installed")

# ---- 3. without the dummy DLL ------------------------------------------------------------------------
dummy_bak <- paste0(dll_local, ".odm_dummy")
gpu_fix_applied <- FALSE
if (file.exists(dll_local) && file.exists(dll_sys) && file.size(dll_local) < 5e6) {
   say("small nvcuda.dll next to OpenMVS (dummy): renamed for test B")
   if (!file.rename(dll_local, dummy_bak)) stop("Cannot rename ", dll_local, " (R must have the rights on C:/ODM)")
   resB <- tryCatch(run_densify_test("B_without_dummy_dll"), error = function(e) { say("test B failed: ", conditionMessage(e)); NULL })
   if (!is.null(resB)) gpu_results <- rbind(gpu_results, resB)
   if (!is.null(resB) && resB$finished && is.finite(resB$gpu_util_max) && resB$gpu_util_max >= 20) {
      gpu_fix_applied <- TRUE
      say("GPU used without the dummy DLL: renaming KEPT (", basename(dummy_bak), "), the next ODM runs use the GPU")
   } else {
      file.rename(dummy_bak, dll_local)
      say("no GPU use without the dummy DLL either: dummy DLL put back")
   }
} else if (file.exists(dummy_bak) && !file.exists(dll_local)) {
   say("the dummy DLL was already renamed by an earlier test (", basename(dummy_bak), ")")
} else {
   say("no dummy nvcuda.dll next to OpenMVS: hypothesis not confirmed, see the OpenMVS messages")
}
write.csv(gpu_results, file.path(gpu_out_dir, "gpu_test_summary.csv"), row.names = FALSE)
print(gpu_results[, c("test", "finished", "minutes", "gpu_util_max", "gpu_mem_max_MB", "nvcuda_loaded")], row.names = FALSE)
