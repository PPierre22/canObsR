#' Does ODM use the NVIDIA GPU?
#'
#' @description
#' Runs for a few minutes the dense matching of OpenMVS (the step of ODM that uses the GPU) on a scene of an existing
#' ODM project, at low resolution, with all its messages, while the NVIDIA GPU is watched; then says whether OpenMVS
#' found and used the GPU and, if not, shows its messages about CUDA. Nothing of the project is modified.
#'
#' Use it when [generate_mosaics()] warns that ODM does not use the GPU (`ODM IS NOT USING THE NVIDIA GPU`), or after
#' installing ODM on a new computer, once a first chunk has passed the alignment (SfM) step. Stop any running ODM
#' first: two programs on the GPU would disturb the test.
#'
#' Frequent causes of an unused GPU: ODM's dummy `nvcuda.dll` (renamed by [canobs_setup()]); a laptop in power saving
#' mode or on battery (Windows: Settings > System > Power > "Best performance", and the profile of the maker's
#' software); a GPU without enough memory for the depth maps (then `downscale_factor_depth_map = 4`).
#'
#' @param project_dir character. An ODM project folder of canobsR (e.g. `out_dir/projects` of [generate_mosaics()],
#'   or `out_dir_project` of [generate_Mosa()]): the first `scene.mvs` found in it is used.
#' @param seconds numeric. Maximum duration of the test.
#' @return Invisibly, a list: `gpu_used`, `gpu_mem_mb`, `gpu_util` (peaks during the test), `cuda_lines` (messages of
#'   OpenMVS about CUDA), `last_lines`, `gpus`.
#' @export
#' @examples
#' \dontrun{
#' canobs_check_odm_gpu("D:/bouamir/2_mosaics/projects")
#' }
canobs_check_odm_gpu <- function(project_dir, seconds = 240) {
   odm <- .find_odm(canobs_config()$odm_path)
   if (is.null(odm)) stop("ODM not found: canobsR::canobs_install_odm()", call. = FALSE)
   sc <- list.files(project_dir, pattern = "^scene\\.mvs$", recursive = TRUE, full.names = TRUE)
   sc <- sc[!grepl("timesift_block", sc)]
   if (!length(sc)) stop("No OpenMVS scene (scene.mvs) in ", project_dir, ": run generate_mosaics() until the ",
                         "dense matching of a first date has started.", call. = FALSE)
   message("OpenMVS test on ", sc[1], " (", seconds, " s at most)...")
   r <- .canobs_call("timesift_odm", "probe_openmvs_gpu", odm_path = odm, scene = normalizePath(sc[1], winslash = "/"),
                     seconds = seconds, .echo = FALSE)
   bar <- strrep("=", 100)
   message(bar)
   if (!length(r$gpus)) {
      .say("no", "no NVIDIA GPU found (nvidia-smi): ODM computes on the processor")
   } else {
      g <- r$gpus[[1]]
      .say("info", "GPU: ", g$name, ", ", round(g$mem_total_mb), " MB",
           if (!is.null(g$power_limit_w)) paste0(", power limit ", g$power_limit_w, " W"))
      if (nzchar(r$power_warning %||% "")) .say("ko", r$power_warning)
      if (!is.null(g$power_limit_w) && g$power_limit_w < 30)
         .say("ko", "power limit of ", g$power_limit_w, " W: the GPU is in power saving mode (laptop on battery, or ",
              "Windows / maker's software in an energy saving profile): plug it in and choose \"Best performance\"")
      if (isTRUE(r$gpu_used)) {
         .say("ok", "OpenMVS uses the GPU: up to ", r$gpu_mem_mb, " MB and ", r$gpu_util, " % during the test")
      } else {
         .say("ko", "OpenMVS does NOT use the GPU (", r$gpu_mem_mb, " MB, ", r$gpu_util, " % during ", r$seconds, " s)")
      }
   }
   if (length(r$cuda_lines)) {
      message("Messages of OpenMVS about CUDA / GPU:")
      message(paste("   ", unlist(r$cuda_lines), collapse = "\n"))
   } else if (length(unlist(r$last_lines)) && any(nzchar(unlist(r$last_lines)))) {
      message("OpenMVS wrote nothing about CUDA / GPU. Its last messages:\n",
              paste("   ", unlist(r$last_lines), collapse = "\n"))
   }
   message(bar)
   invisible(r)
}

`%||%` <- function(a, b) if (is.null(a)) b else a
