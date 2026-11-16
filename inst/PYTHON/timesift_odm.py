# Time-SIFT with OpenDroneMap (ODM), without Metashape and without Docker dependency.
#
# The method is the one of:
#   D. Feurer, F. Vinatier, Joining multi-epoch archival aerial images in a single SfM block
#   allows 3-D change detection with almost exclusively image information, ISPRS Journal of
#   Photogrammetry and Remote Sensing, 146, 2018, 495-506. https://doi.org/10.1016/j.isprsjprs.2018.10.016
#
# The logic follows the ODM plugin written by the same authors (ODM/contrib/time-sift):
#   1. one SfM on ALL images of ALL epochs (ODM stopped after the 'opensfm' stage);
#   2. for each epoch, a copy of the project where the cameras of the other epochs are removed;
#   3. ODM is re-run from the 'openmvs' stage on each copy -> natively co-registered products.
#
# Differences with the ODM plugin:
#   - ODM can be launched natively (run.bat / run.sh), with docker or with apptainer;
#   - images are hard-linked (not copied) and renamed with their epoch as prefix, so that two
#     flights can contain files with the same name;
#   - the per-epoch project is a light copy (no raw images of other epochs, no features/matches);
#   - reconstruction.nvm (used by the texturing step) and images.json are filtered as well;
#   - a run can be resumed.
#
# Only the python standard library is required.

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

IMG_EXT = ('.jpg', '.jpeg', '.png', '.tif', '.tiff')
DATE_PATTERN = r'\d{4}(0[1-9]|1[0-2])([0-2][0-9]|3[01])?'
BLOCK_NAME = 'timesift_block'
ALIGN_QUALITY = {0: 'ultra', 1: 'high', 2: 'medium', 4: 'low', 8: 'lowest'}
DEPTH_QUALITY = {1: 'ultra', 2: 'high', 4: 'medium', 8: 'low', 16: 'lowest'}


def log(msg):
    print(f'[Time-SIFT ODM] {msg}', flush=True)


def _safe(name):
    """Makes a string usable as an ODM project / file name (no space, no dot)."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', name)


def _stem(name):
    """File name without any extension ('a/b/x.JPG.tif' -> 'x')."""
    return os.path.basename(name).split(os.extsep)[0]


def _link_or_copy(src, dst):
    """Hard link (no disk space used); falls back on a copy (other volume, filesystem without links)."""
    if os.path.exists(dst):
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def list_epochs(path_in, group_by_flight=False, folder_names=None):
    """
    Lists the images of each epoch.

    Each sub-folder of path_in is a flight (images are searched recursively). An epoch is a date
    (YYYYMMDD or YYYYMM found in the flight folder name, default) or a flight (group_by_flight).

    Returns a dict {epoch: [(source_path, new_name), ...]}. new_name is unique in the whole block.
    """
    path_in = os.path.abspath(path_in)
    if folder_names is None:
        flights = sorted(d for d in os.listdir(path_in) if os.path.isdir(os.path.join(path_in, d)))
    elif isinstance(folder_names, str):
        flights = [folder_names]
    else:
        flights = list(folder_names)

    epochs, used = {}, set()
    for flight in flights:
        if group_by_flight:
            epoch = _safe(flight)
        else:
            m = re.search(DATE_PATTERN, flight)
            epoch = m.group() if m else _safe(flight)
        if epoch == BLOCK_NAME:
            raise ValueError(f"'{BLOCK_NAME}' cannot be used as a flight name")
        for dirpath, _, filenames in os.walk(os.path.join(path_in, flight)):
            for f in sorted(filenames):
                if not f.lower().endswith(IMG_EXT):
                    continue
                stem, ext = os.path.splitext(f)
                new = f'{epoch}__{_safe(stem)}'
                i = 1
                while new in used:              # same file name in two flights of the same date
                    i += 1
                    new = f'{epoch}__{_safe(stem)}_{i}'
                used.add(new)
                epochs.setdefault(epoch, []).append((os.path.join(dirpath, f), new + ext))
    if not epochs:
        raise ValueError(f'No image found in the sub-folders of {path_in}')
    return epochs


def odm_command(runner, odm_path, datasets_dir, project, options):
    """Builds the command line that runs ODM on datasets_dir/project."""
    datasets_dir = os.path.abspath(datasets_dir)
    if runner == 'native':
        if not odm_path or not os.path.isfile(odm_path):
            raise FileNotFoundError(f'ODM launcher not found: {odm_path} (expected run.bat on Windows, run.sh on Linux)')
        return [odm_path, '--project-path', datasets_dir, project] + options
    if runner == 'docker':
        return ['docker', 'run', '-i', '--rm', '-v', f'{datasets_dir}:/datasets', odm_path or 'opendronemap/odm',
                '--project-path', '/datasets', project] + options
    if runner == 'apptainer':
        if not odm_path:
            raise ValueError('With runner="apptainer", odm_path must be the ODM image (.sif file or docker://opendronemap/odm)')
        return ['apptainer', 'run', '--bind', f'{datasets_dir}:/datasets', odm_path,
                '--project-path', '/datasets', project] + options
    raise ValueError(f"Unknown runner '{runner}' (native, docker or apptainer)")


def run_odm(runner, odm_path, datasets_dir, project, options):
    cmd = odm_command(runner, odm_path, datasets_dir, project, options)
    log('Running: ' + ' '.join(cmd))
    env = dict(os.environ, ODM_NONINTERACTIVE='1')
    # ODM is run from its own folder: it must not inherit the python environment of the caller (conda, reticulate)
    for var in ('PYTHONPATH', 'PYTHONHOME'):
        env.pop(var, None)
    with open(os.path.join(datasets_dir, f'odm_{project}.log'), 'a', encoding='utf-8', errors='replace') as logfile:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, text=True, encoding='utf-8', errors='replace')
        for line in proc.stdout:
            logfile.write(line)
            print(line, end='', flush=True)
        return proc.wait()


def prepare_block(workdir, epochs):
    """Creates workdir/timesift_block/images with (links to) all images, and the images_epochs.txt file."""
    images_dir = os.path.join(workdir, BLOCK_NAME, 'images')
    os.makedirs(images_dir, exist_ok=True)
    with open(os.path.join(workdir, BLOCK_NAME, 'images_epochs.txt'), 'w', encoding='utf-8') as f:
        for epoch, images in epochs.items():
            for src, name in images:
                _link_or_copy(src, os.path.join(images_dir, name))
                f.write(f'{name} {epoch}\n')


def _filter_keys(d, valid_stems):
    for key in list(d):
        if _stem(key) not in valid_stems:
            del d[key]


def filter_reconstruction(path, valid_stems):
    """Removes from an OpenSfM reconstruction.json the shots that do not belong to the epoch."""
    with open(path, encoding='utf-8') as f:
        reconstructions = json.load(f)
    n = 0
    for rec in reconstructions:
        for key in ('shots', 'rig_instances'):
            if key in rec:
                if key == 'rig_instances':
                    # a rig instance is kept if one of its shots belongs to the epoch
                    for inst_id in list(rec[key]):
                        inst = rec[key][inst_id]
                        shots = inst.get('rig_camera_ids', {}) if isinstance(inst, dict) else {}
                        ids = list(shots) + [inst_id]
                        if not any(_stem(i) in valid_stems for i in ids):
                            del rec[key][inst_id]
                else:
                    _filter_keys(rec[key], valid_stems)
        n += len(rec.get('shots', {}))
    shutil.copy(path, path + '.bak')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(reconstructions, f)
    return n


def filter_nvm(path, valid_stems):
    """
    Keeps in reconstruction.nvm (list of cameras used by the texturing step) the cameras of the epoch only.
    The sparse points are dropped (their camera indices would be wrong, and texturing does not use them).
    """
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')
    i = next(k for k, line in enumerate(lines) if k > 0 and line.strip())    # first non-empty line after the header
    n_cam = int(lines[i].split()[0])
    cams = [line for line in lines[i + 1:i + 1 + n_cam] if line.strip() and _stem(line.split()[0]) in valid_stems]
    shutil.copy(path, path + '.bak')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines[:i] + [str(len(cams))] + cams + ['', '0', '', '0', '']))
    return len(cams)


def filter_images_db(path, valid_stems):
    """Keeps in images.json (ODM images database) the photos of the epoch only."""
    with open(path, encoding='utf-8') as f:
        photos = json.load(f)
    photos = [p for p in photos if _stem(p.get('filename', '')) in valid_stems]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(photos, f)
    return len(photos)


def make_epoch_project(workdir, epoch, images):
    """Light copy of the Time-SIFT block, restricted to the cameras of one epoch."""
    block = os.path.join(workdir, BLOCK_NAME)
    dst = os.path.join(workdir, epoch)
    valid = {_stem(name) for _, name in images}
    heavy = {os.path.join(block, 'images'),
             os.path.join(block, 'opensfm', 'features'),
             os.path.join(block, 'opensfm', 'matches'),
             os.path.join(block, 'opensfm', 'undistorted', 'images')}

    def ignore(folder, names):
        return [n for n in names if os.path.join(folder, n) in heavy]

    shutil.copytree(block, dst, ignore=ignore)

    # raw and undistorted images of the epoch: links
    os.makedirs(os.path.join(dst, 'images'))
    for _, name in images:
        _link_or_copy(os.path.join(block, 'images', name), os.path.join(dst, 'images', name))
    und_src = os.path.join(block, 'opensfm', 'undistorted', 'images')
    und_dst = os.path.join(dst, 'opensfm', 'undistorted', 'images')
    os.makedirs(und_dst)
    for f in os.listdir(und_src):
        if _stem(f) in valid:
            _link_or_copy(os.path.join(und_src, f), os.path.join(und_dst, f))

    n = filter_reconstruction(os.path.join(dst, 'opensfm', 'undistorted', 'reconstruction.json'), valid)
    if n == 0:
        raise RuntimeError(f'Epoch {epoch}: none of its {len(images)} images was aligned by the SfM')
    log(f'Epoch {epoch}: {n} aligned cameras out of {len(images)} images')
    nvm = os.path.join(dst, 'opensfm', 'undistorted', 'reconstruction.nvm')
    if os.path.isfile(nvm):
        filter_nvm(nvm, valid)
    db = os.path.join(dst, 'images.json')
    if os.path.isfile(db):
        filter_images_db(db, valid)


def timesift_odm_process(pathDIR,
                         out_dir_ortho,
                         out_dir_DEM=None,
                         out_dir_project=None,
                         resol_ref=0.05,
                         site_name="",
                         group_by_flight=False,
                         downscale_factor_alignement=1,
                         downscale_factor_depth_map=2,
                         suffix="",
                         folder_names=None,
                         odm_path=None,
                         odm_runner="native",
                         odm_args=None,
                         overwrite=False,
                         keep_project=None,
                         ):
    """
    Complete Time-SIFT process with ODM: one orthomosaic (and optionally one DSM) per date or per flight.

    :param str pathDIR: folder with one sub-folder per flight (date as YYYYMMDD or YYYYMM in the folder name).
    :param str out_dir_ortho: folder where the orthomosaics are saved.
    :param str out_dir_DEM: folder where the DSMs are saved (None: no DSM).
    :param str out_dir_project: folder of the ODM projects (working directory). Default: <out_dir_ortho>/_odm_timesift_.
        It should be on the same disk as the images (images are then hard-linked instead of copied).
    :param float resol_ref: resolution (m) of the orthomosaics and DSMs.
    :param str odm_path: native: path of run.bat / run.sh ; docker: image name ; apptainer: image file.
    :param str odm_runner: 'native', 'docker' or 'apptainer'.
    :param list odm_args: additional ODM options, e.g. ['--matcher-neighbors', '16'].
    :param bool overwrite: deletes existing ODM projects (block and epochs) before running. If False, the existing
        SfM block is re-used and the epochs already finished are skipped (resume).
    :param bool keep_project: keeps the ODM projects at the end (default: True if out_dir_project is given).
    :return: dict {epoch: path of the orthomosaic}
    """
    t0 = time.time()
    out_dir_ortho = os.path.abspath(out_dir_ortho)
    os.makedirs(out_dir_ortho, exist_ok=True)
    if out_dir_DEM == "":
        out_dir_DEM = os.path.join(os.path.dirname(out_dir_ortho), "DEM")
    if out_dir_DEM is not None:
        out_dir_DEM = os.path.abspath(out_dir_DEM)
        os.makedirs(out_dir_DEM, exist_ok=True)
    if keep_project is None:
        keep_project = out_dir_project is not None
    workdir = os.path.abspath(out_dir_project) if out_dir_project else os.path.join(out_dir_ortho, '_odm_timesift_')
    if overwrite and os.path.isdir(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir, exist_ok=True)
    if isinstance(odm_args, str):
        odm_args = odm_args.split()

    options = ['--orthophoto-resolution', str(float(resol_ref) * 100),
               '--feature-quality', ALIGN_QUALITY[int(downscale_factor_alignement)],
               '--pc-quality', DEPTH_QUALITY[int(downscale_factor_depth_map)],
               '--orthophoto-compression', 'LZW',
               '--skip-3dmodel', '--skip-report']
    if out_dir_DEM is not None:
        options += ['--dsm', '--dem-resolution', str(float(resol_ref) * 100)]
    options += [str(a) for a in (odm_args or [])]

    epochs = list_epochs(pathDIR, group_by_flight=group_by_flight, folder_names=folder_names)
    log(f'{len(epochs)} epochs: ' + ', '.join(f'{e} ({len(i)} images)' for e, i in epochs.items()))

    # 1. SfM on the multi-temporal block
    sfm_done = os.path.join(workdir, BLOCK_NAME, 'opensfm', 'undistorted', 'reconstruction.json')
    if os.path.isfile(sfm_done):
        log('Existing SfM block found: it is re-used (use overwrite=True to recompute it)')
    else:
        prepare_block(workdir, epochs)
        code = run_odm(odm_runner, odm_path, workdir, BLOCK_NAME, options + ['--end-with', 'opensfm'])
        if not os.path.isfile(sfm_done):
            raise RuntimeError(f'ODM failed during the SfM of the multi-temporal block (exit code {code}). '
                               f'See {os.path.join(workdir, "odm_" + BLOCK_NAME + ".log")}')
    t1 = time.time()
    log(f'SfM on the multi-temporal block done ({t1 - t0:.0f} s)')

    # 2. dense matching, DSM and orthomosaic for each epoch
    results = {}
    for epoch, images in epochs.items():
        te = time.time()
        ortho = os.path.join(workdir, epoch, 'odm_orthophoto', 'odm_orthophoto.tif')
        if os.path.isfile(ortho):
            log(f'Epoch {epoch}: orthomosaic already computed, skipped')
        else:
            if os.path.isdir(os.path.join(workdir, epoch)):
                shutil.rmtree(os.path.join(workdir, epoch))        # unfinished previous run
            make_epoch_project(workdir, epoch, images)
            code = run_odm(odm_runner, odm_path, workdir, epoch, options + ['--rerun-from', 'openmvs'])
            if not os.path.isfile(ortho):
                raise RuntimeError(f'ODM failed for epoch {epoch} (exit code {code}). '
                                   f'See {os.path.join(workdir, "odm_" + epoch + ".log")}')
        out = os.path.join(out_dir_ortho, f'{site_name}{epoch}{suffix}.tif')
        shutil.copy2(ortho, out)
        results[epoch] = out
        if out_dir_DEM is not None:
            dsm = os.path.join(workdir, epoch, 'odm_dem', 'dsm.tif')
            if os.path.isfile(dsm):
                shutil.copy2(dsm, os.path.join(out_dir_DEM, f'{site_name}{epoch}{suffix}.tif'))
            else:
                log(f'WARNING: no DSM produced for epoch {epoch}')
        log(f'Epoch {epoch} finished ({time.time() - te:.0f} s)')

    if not keep_project:
        shutil.rmtree(workdir, ignore_errors=True)
    log(f'Complete pipeline done in {time.time() - t0:.0f} s')
    log('Method: Feurer & Vinatier 2018, https://doi.org/10.1016/j.isprsjprs.2018.10.016')
    return results


def main():
    p = argparse.ArgumentParser(description='Time-SIFT orthomosaics with ODM (one per date or per flight)')
    p.add_argument('--pathDIR', required=True)
    p.add_argument('--out_dir_ortho', required=True)
    p.add_argument('--out_dir_dem', default=None)
    p.add_argument('--out_dir_project', default=None)
    p.add_argument('--resol_ref', type=float, default=0.05)
    p.add_argument('--site_name', default="")
    p.add_argument('--group_by_flight', action='store_true')
    p.add_argument('--downscale_factor_alignement', type=int, default=1, choices=sorted(ALIGN_QUALITY))
    p.add_argument('--downscale_factor_depth_map', type=int, default=2, choices=sorted(DEPTH_QUALITY))
    p.add_argument('--suffix', default="")
    p.add_argument('--odm_path', default=None)
    p.add_argument('--odm_runner', default='native', choices=['native', 'docker', 'apptainer'])
    p.add_argument('--overwrite', action='store_true')
    p.add_argument('--keep_project', action='store_true')
    args, odm_args = p.parse_known_args()      # unknown options are passed to ODM
    timesift_odm_process(pathDIR=args.pathDIR, out_dir_ortho=args.out_dir_ortho, out_dir_DEM=args.out_dir_dem,
                         out_dir_project=args.out_dir_project, resol_ref=args.resol_ref, site_name=args.site_name,
                         group_by_flight=args.group_by_flight,
                         downscale_factor_alignement=args.downscale_factor_alignement,
                         downscale_factor_depth_map=args.downscale_factor_depth_map, suffix=args.suffix,
                         odm_path=args.odm_path, odm_runner=args.odm_runner, odm_args=odm_args,
                         overwrite=args.overwrite, keep_project=True if args.keep_project else None)


# reticulate::source_python() also runs this file as '__main__': the command line is only parsed when it is used
if __name__ == '__main__' and any(a.startswith('--pathDIR') for a in sys.argv[1:]):
    main()
