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
# DensifyPointCloud is run and waited for (exit code kept); in the background, PowerShell writes every second
# the CUDA DLLs the running program has loaded (the nvcuda.dll really used), until it finds some.
read_safe <- function(f) tryCatch(readLines(f, warn = FALSE), error = function(e) character(0))
run_densify_test <- function(label, cuda_device = "-1") {
   wdir <- file.path(gpu_scene_dir, paste0("gputest_", label))      # same depth as ODM's 'depthmaps' folder
   unlink(wdir, recursive = TRUE); dir.create(wdir)
   gpu_log  <- file.path(gpu_out_dir, paste0("gpu_usage_", label, ".csv"))
   mod_file <- file.path(gpu_out_dir, paste0("modules_", label, ".txt"))
   out_txt  <- file.path(gpu_out_dir, paste0("densify_", label, "_console.txt"))
   unlink(c(gpu_log, mod_file, out_txt))
   apps_log <- file.path(gpu_out_dir, paste0("gpu_apps_", label, ".txt"))
   stop_file <- file.path(gpu_out_dir, paste0("stop_", label))
   unlink(c(apps_log, stop_file))
   # every 0.5 s: GPU use, programs using the GPU, CUDA DLLs loaded by DensifyPointCloud (appended at once, no buffer)
   system2("powershell", c("-NoProfile", "-Command", shQuote(sprintf(paste0(
      "while (-not (Test-Path '%s')) { ",
      "& '%s' --query-gpu=timestamp,utilization.gpu,memory.used --format=csv,noheader,nounits | Add-Content -Path '%s'; ",
      "& '%s' --query-compute-apps=pid,process_name,used_memory --format=csv,noheader | Add-Content -Path '%s'; ",
      "$m = (Get-Process DensifyPointCloud -ErrorAction SilentlyContinue).Modules | ",
      "Where-Object { $_.ModuleName -match '^(nvcuda|cudart)' } | ForEach-Object { $_.FileName }; ",
      "if ($m) { $m | Add-Content -Path '%s' }; Start-Sleep -Milliseconds 500 }"),
      win(stop_file), win(smi), win(gpu_log), win(smi), win(apps_log), win(mod_file)))), wait = FALSE)
   Sys.sleep(3)
   args <- c(shQuote(win(scene)), "--resolution-level", "2",
             "--dense-config-file", shQuote(win(file.path(gpu_scene_dir, "Densify.ini"))),
             "--max-resolution", "5472", "--min-resolution", "640", "--number-views-fuse", "2",
             "--sub-resolution-levels", "2", "--archive-type", "3",
             "-w", shQuote(win(wdir)), "-o", shQuote(win(file.path(wdir, "scene_dense_test.mvs"))),
             "-v", "2", "--cuda-device", cuda_device)
   say("test ", label, ": DensifyPointCloud started")
   t0 <- Sys.time()
   status <- suppressWarnings(system2(densify, args, stdout = out_txt, stderr = out_txt, wait = TRUE,
                                      timeout = gpu_timeout_min * 60))
   minutes <- round(as.numeric(difftime(Sys.time(), t0, units = "mins")), 1)
   Sys.sleep(2)
   writeLines("stop", stop_file); Sys.sleep(3); unlink(stop_file)
   g <- tryCatch(read.csv(gpu_log, header = FALSE, col.names = c("time", "util", "mem"), strip.white = TRUE),
                 error = function(e) data.frame(util = NA_real_, mem = NA_real_))
   ok <- file.exists(file.path(wdir, "scene_dense_test.mvs"))
   omvs_logs <- list.files(wdir, pattern = "\\.log$", full.names = TRUE)
   for (f in omvs_logs) file.copy(f, file.path(gpu_out_dir, paste0(label, "_", basename(f))), overwrite = TRUE)
   txt <- unlist(lapply(c(omvs_logs, out_txt), read_safe))
   cuda_lines <- unique(grep("cuda|gpu|device|error|fail", txt, ignore.case = TRUE, value = TRUE))
   modules <- unique(trimws(read_safe(mod_file))); modules <- modules[nzchar(modules)]
   apps <- unique(grep("Densify", read_safe(apps_log), value = TRUE, ignore.case = TRUE))
   res <- data.frame(test = label, exit_code = as.integer(status), finished = ok, minutes = minutes,
                     gpu_util_max = if (all(is.na(g$util))) NA else max(g$util, na.rm = TRUE),
                     gpu_mem_max_MB = if (all(is.na(g$mem))) NA else max(g$mem, na.rm = TRUE),
                     nvcuda_loaded = if (length(modules)) paste(modules, collapse = " ; ") else "not seen",
                     densify_on_gpu = length(apps) > 0,
                     messages = paste(head(trimws(cuda_lines), 8), collapse = " | "))
   say("test ", label, ": exit code ", res$exit_code, ", ", minutes, " min, finished = ", ok, ", GPU max ",
       res$gpu_util_max, " %, GPU memory max ", res$gpu_mem_max_MB, " MB, DensifyPointCloud listed by nvidia-smi: ",
       res$densify_on_gpu, ", CUDA DLLs loaded: ", res$nvcuda_loaded)
   say("test ", label, ": last lines of OpenMVS: ", paste(tail(trimws(txt[nzchar(trimws(txt))]), 6), collapse = " | "))
   if (nzchar(res$messages)) say("test ", label, ": CUDA / error messages: ", res$messages)
   unlink(wdir, recursive = TRUE)                                   # the ODM project is left as it was
   res
}

if (any(grepl("DensifyPointCloud", system("tasklist", intern = TRUE))))
   stop("A DensifyPointCloud is already running (ODM?): run this test when it has finished")
say("cudart64_12.dll shipped with ODM: ", dll_info(file.path(odm_bin, "cudart64_12.dll")))
gpu_results <- run_densify_test("A_as_installed")

# ---- 3. without the dummy DLL ------------------------------------------------------------------------
dummy_bak <- paste0(dll_local, ".odm_dummy")
gpu_fix_applied <- FALSE
if (file.exists(dll_local) && file.exists(dll_sys) && file.size(dll_local) < 5e6) {
   say("small nvcuda.dll next to OpenMVS (dummy): renamed for test B")
   if (!file.rename(dll_local, dummy_bak)) stop("Cannot rename ", dll_local, " (R must have the rights on C:/ODM)")
   resB <- tryCatch(run_densify_test("B_without_dummy_dll"), error = function(e) { say("test B failed: ", conditionMessage(e)); NULL })
   if (!is.null(resB)) gpu_results <- rbind(gpu_results, resB)
   # same run forced on the CPU (--cuda-device -2): reference time
   resC <- tryCatch(run_densify_test("C_without_dummy_dll_CPU_only", cuda_device = "-2"),
                    error = function(e) { say("test C failed: ", conditionMessage(e)); NULL })
   if (!is.null(resC)) gpu_results <- rbind(gpu_results, resC)
   gpu_used <- !is.null(resB) && resB$finished &&
      (isTRUE(resB$densify_on_gpu) || isTRUE(resB$gpu_util_max >= 20) ||
       (!is.null(resC) && resC$finished && resB$minutes < 0.6 * resC$minutes))
   if (!is.null(resB) && !is.null(resC)) say("time on GPU (B) ", resB$minutes, " min, on CPU only (C) ", resC$minutes, " min")
   if (gpu_used) {
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
print(gpu_results[, c("test", "exit_code", "finished", "minutes", "gpu_util_max", "gpu_mem_max_MB", "densify_on_gpu")], row.names = FALSE)
