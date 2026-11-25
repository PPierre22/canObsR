# =====================================================================================================
# ODM: effect of the density of the surface used for the orthomosaics (--mesh-size)
# =====================================================================================================
# Metashape (canobsR pipeline) projects the images on a DEM at resol_ref (5 cm). ODM projects them on a 2.5D mesh
# made from a DSM at about resol_ref, then simplified to at most --mesh-size vertices (ODM default: 200000, i.e. about
# one vertex every 1.5 m on the 45 ha of the test data). On tree crowns, a coarser surface can move or blur the edges
# of the crowns in the orthomosaic. This script runs ODM again from the meshing step only (same alignment, same point
# cloud) with denser meshes, then compares each series with Metashape (same AROSICS comparison as the benchmark).
#
# To run AFTER benchmark_engines.R (it reuses bench_dir/project_odm and bench_dir/ortho_metashape).
# Each value of mesh_sizes: meshing + texturing + orthomosaic of each date (no new SfM, no new dense matching).
# =====================================================================================================

lib_dev <- "D:/canobsR/Rlib_dev"
library(canobsR, lib.loc = lib_dev)
library(reticulate)
library(terra)
use_condaenv("C:/ProgramData/miniconda3/envs/arosics", required = TRUE)

my_path    <- "D:/canobsR/data/canobsR_data/canobsR_data"
path_in    <- file.path(my_path, "1_my_drone_data")
bench_dir  <- file.path(my_path, "benchmark3")
odm_path   <- "C:/ODM/run.bat"
mesh_sizes <- c(1000000, 4000000)          # ODM default: 200000 (series ortho_odm of the benchmark)
common <- list(path_in = path_in, resol_ref = 0.05, site_name = "Bouamir", crs = "EPSG::32633")

times <- data.frame(mesh_size = numeric(0), minutes = numeric(0))
for (m in mesh_sizes) {
   out <- file.path(bench_dir, paste0("ortho_odm_mesh", m))
   t <- system.time(do.call(generate_Mosa, c(common, list(
      out_dir_ortho = out, out_dir_project = file.path(bench_dir, "project_odm"), engine = "odm", odm_path = odm_path,
      odm_args = c("--mesh-size", format(m, scientific = FALSE)), tempdir_custom = file.path(bench_dir, "tmp")))))
   times <- rbind(times, data.frame(mesh_size = m, minutes = round(t[["elapsed"]] / 60, 1)))
   write.csv(times, file.path(bench_dir, "mesh_size_times.csv"), row.names = FALSE)
}
# the project is left with the last mesh size: the next run with the default mesh size only redoes the meshing
print(times, row.names = FALSE)

# comparison with Metashape, for each series (same script as the benchmark)
py_file <- "D:/canobsR/compare_engines.py"
dir_ref <- file.path(bench_dir, "ortho_metashape")
for (m in c(200000, mesh_sizes)) {
   dir_odm <- if (m == 200000) file.path(bench_dir, "ortho_odm") else file.path(bench_dir, paste0("ortho_odm_mesh", m))
   dir_out <- file.path(bench_dir, paste0("compare_mesh", m))
   tmp_dir <- file.path(bench_dir, "tmp_compare")
   source("D:/canobsR/compare_Mosa_engines.R")
}
cat("\nSee compare_mesh*/summary.txt: shifts ODM vs Metashape for each mesh size.\n")
