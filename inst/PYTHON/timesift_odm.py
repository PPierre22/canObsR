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
#   - images are hard-linked (not copied) and renamed with their epoch as prefix ("<epoch>__<name>"),
#     so that two flights can contain files with the same name, and so that the epoch of every
#     image can be checked at any step;
#   - every file of the epoch project that lists images is filtered (the plugin only filters
#     undistorted/reconstruction.json): reconstruction.json, undistorted/reconstruction.json,
#     undistorted/tracks.csv, undistorted/reconstruction.nvm, images.json;
#   - after each epoch, the project is checked: no image of another epoch may be found in it;
#   - optional colour calibration of the images on the tie points of the whole multi-temporal block
#     (equivalent of Metashape calibrateColors(), used by the Metashape pipeline of canObsR);
#   - a run can be resumed; time spent in each ODM stage is reported.
#
# The python standard library is enough, except for the colour calibration (numpy + pillow or gdal).

import argparse
import hashlib
import json
import mmap
import os
import re
import shutil
import struct
import subprocess
import sys
import time

IMG_EXT = ('.jpg', '.jpeg', '.png', '.tif', '.tiff')
DATE_PATTERN = r'\d{4}(0[1-9]|1[0-2])([0-2][0-9]|3[01])?'
BLOCK_NAME = 'timesift_block'
SEP = '__'                      # separator between the epoch and the original image name
RESERVED = {BLOCK_NAME, 'time-sift-block'}
PARAMS_FILE = 'canobs_timesift_params.json'
TIMINGS_FILE = 'timings.csv'
TMP_NAME = '_tmp'               # default folder of the temporary files, created by generate_Mosa() in out_dir_project
COLOR_FILE = 'color_gains.csv'

# Metashape downscale factors -> ODM quality levels, at the same image resolution:
# alignment: Metashape 1 (High) = original images; ODM feature-quality ultra = original images (1),
#            high = 1/2, medium = 1/4, low = 1/8, lowest = 1/16 (ODM caps feature images at 4480 px)
ALIGN_QUALITY = {0: 'ultra', 1: 'ultra', 2: 'high', 4: 'medium', 8: 'low'}
# depth maps: Metashape 1 (Ultra high) = original images, 2 (High) = 1/2, 4 (Medium) = 1/4 ...;
#             ODM pc-quality ultra = 1/2 of the images, high = 1/4, medium = 1/8, low = 1/16
#             (ODM has no level at full resolution: 1 and 2 both give 'ultra')
DEPTH_QUALITY = {1: 'ultra', 2: 'ultra', 4: 'high', 8: 'medium', 16: 'low'}


def log(msg):
    print(f'[Time-SIFT ODM] {msg}', flush=True)


class TimeSiftError(Exception):
    pass


def _safe(name):
    """Makes a string usable as an ODM project / file name (no space, no dot)."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', name)


def _stem(name):
    """File name without any extension ('a/b/x.JPG.tif' -> 'x')."""
    return os.path.basename(str(name)).split(os.extsep)[0]


def epoch_of(name):
    """Epoch of a renamed image ('20220427__DJI_0042.JPG.tif' -> '20220427'), None if not renamed."""
    stem = _stem(name)
    return stem.split(SEP)[0] if SEP in stem else None


def _link_or_copy(src, dst):
    """Hard link (no disk space used); falls back on a copy (other volume, filesystem without links)."""
    if os.path.exists(dst):
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


# ODM / OpenSfM files are read as bytes and decoded as UTF-8 with 'surrogateescape': bytes that are not
# valid UTF-8 (e.g. a camera name or a path written in the Windows code page) are kept unchanged when the
# file is written back. Image names, the only thing used here, are ASCII.
ENC = dict(encoding='utf-8', errors='surrogateescape', newline='')


def _read_json(path):
    with open(path, 'rb') as f:
        return json.loads(f.read().decode('utf-8', 'surrogateescape'))


def _write_json(obj, path):
    """JSON written as ASCII, except the original undecodable bytes, which are written back unchanged."""
    out = bytearray()
    for ch in json.dumps(obj, ensure_ascii=False):
        o = ord(ch)
        if o < 128:
            out.append(o)
        elif 0xDC80 <= o <= 0xDCFF:          # undecodable byte of the original file
            out.append(o - 0xDC00)
        elif o > 0xFFFF:
            o -= 0x10000
            out += ('\\u%04x\\u%04x' % (0xD800 + (o >> 10), 0xDC00 + (o & 0x3FF))).encode()
        else:
            out += ('\\u%04x' % o).encode()
    with open(path, 'wb') as f:
        f.write(bytes(out))


# OpenSfM tracks.csv. Two formats exist:
#  - text (versions 0 to 2): one observation per line, tab separated: image, track, feature, x, y, [scale,] r, g, b ...
#  - binary (version 102, OpenSfM of ODM >= 3.6, opensfm/src/lib/map/src/tracks_manager.cc): after the header line,
#    for each observation: uint16 image name length, uint16 track id length, image name, track id, then the
#    20-byte struct {int32 feature; float32 x, y, scale; uint8 r, g, b; 1 padding byte}.
TRACK_LEN = struct.Struct('<HH')
TRACK_REC = struct.Struct('<ifffBBBx')


def iter_tracks(path):
    """Yields (image, track id, (r, g, b), raw bytes of the observation) for both formats; returns the header."""
    with open(path, 'rb') as f:
        header = f.readline()
        m = re.search(rb'_v(\d+)', header)
        version = int(m.group(1)) if m else 0
        if version >= 100:
            size = os.fstat(f.fileno()).st_size
            if size <= len(header):
                return
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                pos, n = len(header), len(mm)
                while pos + TRACK_LEN.size <= n:
                    start = pos
                    il, tl = TRACK_LEN.unpack_from(mm, pos)
                    pos += TRACK_LEN.size
                    if pos + il + tl + TRACK_REC.size > n:
                        break
                    image = mm[pos:pos + il].decode('utf-8', 'replace')
                    track = mm[pos + il:pos + il + tl].decode('utf-8', 'replace')
                    pos += il + tl
                    rec = TRACK_REC.unpack_from(mm, pos)
                    pos += TRACK_REC.size
                    yield image, track, rec[4:7], mm[start:pos]
            finally:
                mm.close()
        else:
            icol = 5 if version == 0 else 6
            for line in f:
                cells = line.rstrip(b'\r\n').split(b'\t')
                if len(cells) < icol + 3:
                    continue
                try:
                    rgb = tuple(int(float(c)) for c in cells[icol:icol + 3])
                except ValueError:
                    rgb = (0, 0, 0)
                yield cells[0].decode('utf-8', 'replace'), cells[1].decode('utf-8', 'replace'), rgb, line


def tracks_header(path):
    with open(path, 'rb') as f:
        return f.readline()


# ---------------------------------------------------------------------------------------------
# 1. Images and epochs
# ---------------------------------------------------------------------------------------------

def exif_info(path, nbytes=131072):
    """
    Date-time of acquisition ('YYYY:MM:DD HH:MM:SS', DateTimeOriginal, else DateTime) and presence of GPS tags,
    read with Pillow if available; otherwise the first date-time found in the image header. (None, None) if unknown.
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            exif = im.getexif()
            dt = exif.get_ifd(0x8769).get(36867) or exif.get(306)       # DateTimeOriginal, DateTime
            gps = bool(exif.get_ifd(0x8825))
        if dt:
            return str(dt).strip('\x00 '), gps
    except Exception:
        pass
    try:
        with open(path, 'rb') as f:
            head = f.read(nbytes)
    except OSError:
        return None, None
    m = re.search(rb'(?:19|20)\d{2}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}', head)
    return (m.group().decode(), None) if m else (None, None)


def exif_date(path):
    """Day of acquisition (YYYYMMDD) from the EXIF, None if not found."""
    dt, _ = exif_info(path)
    return dt[:10].replace(':', '') if dt else None


def _fingerprint(path, nbytes=262144):
    """Size + hash of the beginning of the file (EXIF time stamp and GPS make it unique per shot)."""
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        h.update(f.read(nbytes))
    return f'{os.path.getsize(path)}-{h.hexdigest()}'


def list_epochs(path_in, group_by_flight=False, folder_names=None, check_dates=True, check_duplicates=True):
    """
    Lists the images of each epoch.

    Each sub-folder of path_in is a flight (images are searched recursively in it). An epoch is a date
    (YYYYMMDD or YYYYMM found in the flight folder name, default) or a flight (group_by_flight).
    Without group_by_flight, sub-folders without a date in their name are ignored (with a warning).

    The folder defines the epoch: all its images make one mosaic, whatever their EXIF dates (a flight can be
    finished on another day, e.g. after rain). With check_dates, the EXIF days found in each folder are only
    reported (no error); see flights_report() / check_flights() for a complete check before processing.

    Error: the same image file found twice (check_duplicates), e.g. a copy of the images in a working folder
    inside path_in: it would be used twice, possibly in two epochs.

    Returns a dict {epoch: [(source_path, new_name), ...]}, new_name = "<epoch>__<original name>".
    """
    path_in = os.path.abspath(path_in)
    if folder_names is None:
        flights = sorted(d for d in os.listdir(path_in) if os.path.isdir(os.path.join(path_in, d)))
    elif isinstance(folder_names, str):
        flights = [folder_names]
    else:
        flights = list(folder_names)

    epochs, used, ignored = {}, set(), []
    days, seen, duplicates = {}, {}, []
    for flight in flights:
        if flight in RESERVED or flight.startswith(('.', '_')):
            ignored.append(flight)
            continue
        m = re.search(DATE_PATTERN, flight)
        if group_by_flight:
            epoch = _safe(flight)
        elif m:
            epoch = m.group()
        else:
            ignored.append(flight)
            continue
        for dirpath, dirnames, filenames in os.walk(os.path.join(path_in, flight)):
            dirnames[:] = sorted(d for d in dirnames if d not in RESERVED)
            for f in sorted(filenames):
                if not f.lower().endswith(IMG_EXT):
                    continue
                src = os.path.join(dirpath, f)
                if check_duplicates:
                    fp = _fingerprint(src)
                    if fp in seen:
                        duplicates.append((seen[fp], src))
                        continue
                    seen[fp] = src
                if check_dates:
                    d = exif_date(src) or 'unknown'
                    days.setdefault(flight, {}).setdefault(d, 0)
                    days[flight][d] += 1
                stem, ext = os.path.splitext(f)
                new = f'{epoch}{SEP}{_safe(stem)}'
                i = 1
                while new in used:              # same file name in two flights of the same date
                    i += 1
                    new = f'{epoch}{SEP}{_safe(stem)}_{i}'
                used.add(new)
                epochs.setdefault(epoch, []).append((src, new + ext))

    if ignored:
        log('WARNING: sub-folders ignored (no date in their name, or reserved name): ' + ', '.join(ignored))
    if duplicates:
        raise TimeSiftError(f'{len(duplicates)} image(s) found twice in {path_in}, e.g.:\n  '
                            + '\n  '.join(f'{a}  ==  {b}' for a, b in duplicates[:5])
                            + '\nRemove the copies (each image must be in the folder of its flight only).')
    for flight, d in days.items():                     # information only: the folder defines the epoch
        m = re.search(DATE_PATTERN, flight)
        if len(d) > 1 or (m and not all(k.startswith(m.group()) for k in d)):
            log(f'Note: {flight}: images taken on ' + ', '.join(f'{k} ({n})' for k, n in sorted(d.items()))
                + ' -> all used for the mosaic of this folder')
    if not epochs:
        raise TimeSiftError(f'No image found in dated sub-folders of {path_in}')
    for epoch in epochs:
        if any(epoch != e and (epoch + SEP).startswith(e + SEP) for e in epochs):
            raise TimeSiftError(f'Ambiguous epoch names: {epoch}')
    return epochs


def flights_report(path_in, folder_names=None, check_duplicates=True):
    """
    Report on the image folders before processing (no error, no change). For each image: folder, epoch as
    generate_Mosa() would build it, EXIF date-time, GPS tags, identical copies elsewhere.
    Returns a list of dicts (one per image) and a list of dicts (one per folder).
    """
    path_in = os.path.abspath(path_in)
    if folder_names is None:
        flights = sorted(d for d in os.listdir(path_in) if os.path.isdir(os.path.join(path_in, d)))
    else:
        flights = [folder_names] if isinstance(folder_names, str) else list(folder_names)
    images, seen = [], {}
    for flight in flights:
        m = re.search(DATE_PATTERN, flight)
        for dirpath, dirnames, filenames in os.walk(os.path.join(path_in, flight)):
            dirnames[:] = sorted(dirnames)
            for f in sorted(filenames):
                if not f.lower().endswith(IMG_EXT):
                    continue
                src = os.path.join(dirpath, f)
                dt, gps = exif_info(src)
                rec = {'folder': flight, 'file': os.path.relpath(src, path_in),
                       'folder_date': m.group() if m else None,
                       'exif_datetime': dt, 'exif_day': dt[:10].replace(':', '') if dt else None,
                       'gps': gps, 'duplicate_of': None}
                if check_duplicates:
                    fp = _fingerprint(src)
                    if fp in seen:
                        rec['duplicate_of'] = seen[fp]
                    else:
                        seen[fp] = rec['file']
                images.append(rec)
    folders = []
    for flight in flights:
        im = [r for r in images if r['folder'] == flight]
        m = re.search(DATE_PATTERN, flight)
        ignored = flight in RESERVED or flight.startswith(('.', '_')) or not m
        d = {}
        for r in im:
            d[r['exif_day'] or 'unknown'] = d.get(r['exif_day'] or 'unknown', 0) + 1
        known = sorted(k for k in d if k != 'unknown')
        times = sorted(r['exif_datetime'] for r in im if r['exif_datetime'])
        notes = []
        if ignored:
            notes.append('ignored by generate_Mosa (no date in the folder name, or reserved name)')
        if not im:
            notes.append('no image')
        if 'unknown' in d:
            notes.append(f"{d['unknown']} image(s) without EXIF date")
        if len(known) > 1:
            notes.append(f'images taken on {len(known)} days (all used for one mosaic)')
        if m and known and not any(k.startswith(m.group()) for k in known):
            notes.append('no image taken on the date of the folder name')
        elif m and known and not all(k.startswith(m.group()) for k in known):
            notes.append('some images taken on another day than the folder date')
        n_dup = sum(r['duplicate_of'] is not None for r in im)
        if n_dup:
            notes.append(f'{n_dup} image(s) identical to another file (generate_Mosa would stop)')
        n_nogps = sum(r['gps'] is False for r in im)
        if n_nogps:
            notes.append(f'{n_nogps} image(s) without GPS tags')
        folders.append({'folder': flight, 'folder_date': m.group() if m else None,
                        'epoch': None if ignored else m.group(), 'n_images': len(im),
                        'exif_days': '; '.join(f'{k} ({n})' for k, n in sorted(d.items())),
                        'n_days': len(known), 'first_shot': times[0] if times else None,
                        'last_shot': times[-1] if times else None,
                        'n_duplicates': n_dup, 'n_no_gps': n_nogps,
                        'status': 'ok' if not notes else ('ignored' if ignored else 'check'),
                        'notes': '; '.join(notes)})
    epochs = [f['epoch'] for f in folders if f['epoch']]
    for f in folders:
        if f['epoch'] and epochs.count(f['epoch']) > 1:
            f['notes'] = '; '.join(x for x in (f['notes'], 'other folder(s) with the same date: merged into one mosaic') if x)
            f['status'] = 'check'
    return images, folders


# ---------------------------------------------------------------------------------------------
# 2. Running ODM
# ---------------------------------------------------------------------------------------------

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


STAGE_RE = re.compile(r'\[INFO\]\s+(Running|Finished) (\S+) stage')
GPU_RE = re.compile(r'(?i)\b(gpu|cuda)\b')


def run_odm(runner, odm_path, datasets_dir, project, options, tmp_dir=None):
    """Runs ODM, writes its log in datasets_dir/odm_<project>.log and the time spent in each stage in timings.csv."""
    cmd = odm_command(runner, odm_path, datasets_dir, project, options)
    log('Running: ' + ' '.join(cmd))
    env = dict(os.environ, ODM_NONINTERACTIVE='1')
    # ODM is run with its own python: it must not inherit the python environment of the caller (conda, reticulate)
    for var in ('PYTHONPATH', 'PYTHONHOME'):
        env.pop(var, None)
    if tmp_dir:                                # temporary files of ODM and of the tools it runs (GDAL, PDAL...)
        os.makedirs(tmp_dir, exist_ok=True)
        env.update(TMP=tmp_dir, TEMP=tmp_dir, TMPDIR=tmp_dir, CPL_TMPDIR=tmp_dir)
    started, gpu_lines = {}, []
    timings = os.path.join(datasets_dir, TIMINGS_FILE)
    new_file = not os.path.isfile(timings)
    with open(os.path.join(datasets_dir, f'odm_{project}.log'), 'a', encoding='utf-8', errors='replace') as logfile, \
            open(timings, 'a', encoding='utf-8') as tfile:
        if new_file:
            tfile.write('project,stage,seconds,start,end\n')
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, text=True, encoding='utf-8', errors='replace')
        for line in proc.stdout:
            logfile.write(line)
            print(line, end='', flush=True)
            m = STAGE_RE.search(line)
            if m:
                if m.group(1) == 'Running':
                    started[m.group(2)] = time.time()
                elif m.group(2) in started:
                    t_start, t_end = started.pop(m.group(2)), time.time()
                    fmt = '%Y-%m-%d %H:%M:%S'
                    tfile.write(f'{project},{m.group(2)},{t_end - t_start:.0f},'
                                f'{time.strftime(fmt, time.localtime(t_start))},{time.strftime(fmt, time.localtime(t_end))}\n')
                    tfile.flush()
            elif GPU_RE.search(line) and len(gpu_lines) < 5 and line.strip() not in gpu_lines:
                gpu_lines.append(line.strip())
        code = proc.wait()
    if gpu_lines:
        log('GPU information found in the ODM log: ' + ' | '.join(gpu_lines))
    return code


def timings_summary(workdir):
    """Time spent per ODM stage: SfM block, and sum over the epochs."""
    path = os.path.join(workdir, TIMINGS_FILE)
    if not os.path.isfile(path):
        return
    block, epochs = {}, {}
    with open(path, encoding='utf-8') as f:
        next(f)
        for line in f:
            project, stage, sec = line.strip().split(',')[:3]
            target = block if project == BLOCK_NAME else epochs
            target[stage] = target.get(stage, 0) + float(sec)
    log('Time spent per ODM stage (minutes):')
    for name, d in (('SfM block', block), ('all epochs', epochs)):
        if d:
            log(f'  {name}: ' + ', '.join(f'{s} {v / 60:.1f}' for s, v in d.items() if v >= 30))


# ---------------------------------------------------------------------------------------------
# 3. Epoch projects
# ---------------------------------------------------------------------------------------------

def prepare_block(workdir, epochs):
    """Creates workdir/timesift_block/images with (links to) all images, and the images_epochs.txt file."""
    images_dir = os.path.join(workdir, BLOCK_NAME, 'images')
    os.makedirs(images_dir, exist_ok=True)
    with open(os.path.join(workdir, BLOCK_NAME, 'images_epochs.txt'), 'w', encoding='utf-8') as f:
        for epoch, images in epochs.items():
            for src, name in images:
                _link_or_copy(src, os.path.join(images_dir, name))
                f.write(f'{name} {epoch}\n')


def read_images_epochs(workdir):
    """{epoch: set of image stems} from timesift_block/images_epochs.txt."""
    out = {}
    with open(os.path.join(workdir, BLOCK_NAME, 'images_epochs.txt'), encoding='utf-8') as f:
        for line in f:
            if line.strip():
                name, epoch = line.split()
                out.setdefault(epoch, set()).add(_stem(name))
    return out


def filter_reconstruction(path, valid_stems, keep_points=None):
    """
    Removes from an OpenSfM reconstruction.json the shots (and rig instances) of the other epochs.
    keep_points: if given, only the 3D points whose track id is in this set are kept.
    """
    reconstructions = _read_json(path)
    n = 0
    for rec in reconstructions:
        if keep_points is not None and 'points' in rec:
            rec['points'] = {k: v for k, v in rec['points'].items() if str(k) in keep_points}
        shots = rec.get('shots', {})
        for key in list(shots):
            if _stem(key) not in valid_stems:
                del shots[key]
        instances = rec.get('rig_instances', {})
        for inst_id in list(instances):
            inst = instances[inst_id]
            ids = list(inst.get('rig_camera_ids', {})) if isinstance(inst, dict) else []
            if not any(_stem(i) in valid_stems for i in ids + [inst_id]):
                del instances[inst_id]
        n += len(shots)
    shutil.copy(path, path + '.bak')
    _write_json(reconstructions, path)
    return n


def filter_tracks(path, valid_stems):
    """
    Keeps in an OpenSfM tracks.csv the observations of the epoch only.
    Without it, the observations of the other epochs are given to the first image of the epoch when the
    sparse points are exported to OpenMVS (std::map default value in OpenSfM openmvs_exporter.h).
    Returns the ids of the tracks seen in at least 2 images of the epoch.
    """
    tmp = path + '.tmp'
    n_obs = {}
    with open(tmp, 'wb') as fout:
        fout.write(tracks_header(path))
        for image, track, _, raw in iter_tracks(path):
            if _stem(image) in valid_stems:
                fout.write(raw)
                n_obs[track] = n_obs.get(track, 0) + 1
    shutil.move(path, path + '.bak')
    shutil.move(tmp, path)
    return {t for t, n in n_obs.items() if n >= 2}


def filter_nvm(path, valid_stems):
    """
    Keeps in reconstruction.nvm (list of cameras used by the texturing step) the cameras of the epoch only.
    The sparse points are dropped (their camera indices would be wrong, and texturing does not use them).
    """
    with open(path, **ENC) as f:
        lines = f.read().split('\n')
    i = next(k for k, line in enumerate(lines) if k > 0 and line.strip())    # first non-empty line after the header
    n_cam = int(lines[i].split()[0])
    cams = [line for line in lines[i + 1:i + 1 + n_cam] if line.strip() and _stem(line.split()[0]) in valid_stems]
    shutil.copy(path, path + '.bak')
    eol = '\r' if lines[0].endswith('\r') else ''
    with open(path, 'w', **ENC) as f:
        f.write('\n'.join(lines[:i] + [str(len(cams)) + eol] + cams + [eol, '0' + eol, eol, '0' + eol, '']))
    return len(cams)


def filter_images_db(path, valid_stems):
    """Keeps in images.json (ODM images database) the photos of the epoch only."""
    photos = [p for p in _read_json(path) if _stem(p.get('filename', '')) in valid_stems]
    _write_json(photos, path)
    return len(photos)


def make_epoch_project(workdir, epoch, images, gains=None):
    """Light copy of the Time-SIFT block, restricted to the cameras of one epoch."""
    block = os.path.join(workdir, BLOCK_NAME)
    dst = os.path.join(workdir, epoch)
    valid = {_stem(name) for _, name in images}
    heavy = {os.path.join(block, 'images'),
             os.path.join(block, 'opensfm', 'features'),
             os.path.join(block, 'opensfm', 'matches'),
             os.path.join(block, 'opensfm', 'undistorted', 'images')}

    def ignore(folder, names):
        return [n for n in names if os.path.join(folder, n) in heavy or n.endswith('.bak')]

    shutil.copytree(block, dst, ignore=ignore)
    # ODM considers that feature detection and matching are done if these folders exist (opendm/osfm.py,
    # is_feature_matching_done). Their content is not used after the SfM: empty folders are enough, otherwise
    # ODM detects and matches the features again for each epoch.
    for d in ('features', 'matches'):
        os.makedirs(os.path.join(dst, 'opensfm', d), exist_ok=True)

    # raw images of the epoch: links
    os.makedirs(os.path.join(dst, 'images'))
    for _, name in images:
        _link_or_copy(os.path.join(block, 'images', name), os.path.join(dst, 'images', name))
    # undistorted images of the epoch (the ones used by OpenMVS and by the texturing): links, or colour-corrected copies
    und_src = os.path.join(block, 'opensfm', 'undistorted', 'images')
    und_dst = os.path.join(dst, 'opensfm', 'undistorted', 'images')
    os.makedirs(und_dst)
    for f in os.listdir(und_src):
        if _stem(f) in valid:
            if gains is not None and _stem(f) in gains:
                apply_gain(os.path.join(und_src, f), os.path.join(und_dst, f), gains[_stem(f)])
            else:
                _link_or_copy(os.path.join(und_src, f), os.path.join(und_dst, f))

    osfm = os.path.join(dst, 'opensfm')
    # tracks first: the 3D points kept are the ones seen in at least 2 images of the epoch (OpenSfM
    # export_openmvs fails on a point whose track is not in tracks.csv)
    tracks = os.path.join(osfm, 'undistorted', 'tracks.csv')
    keep = filter_tracks(tracks, valid) if os.path.isfile(tracks) else None
    n = filter_reconstruction(os.path.join(osfm, 'undistorted', 'reconstruction.json'), valid, keep_points=keep)
    if n == 0:
        raise TimeSiftError(f'Epoch {epoch}: none of its {len(images)} images was aligned by the SfM')
    log(f'Epoch {epoch}: {n} aligned cameras out of {len(images)} images')
    if os.path.isfile(os.path.join(osfm, 'reconstruction.json')):
        filter_reconstruction(os.path.join(osfm, 'reconstruction.json'), valid)
    if os.path.isfile(os.path.join(osfm, 'undistorted', 'reconstruction.nvm')):
        filter_nvm(os.path.join(osfm, 'undistorted', 'reconstruction.nvm'), valid)
    if os.path.isfile(os.path.join(dst, 'images.json')):
        filter_images_db(os.path.join(dst, 'images.json'), valid)


# ---------------------------------------------------------------------------------------------
# 4. Checks: no image of another epoch in an epoch project
# ---------------------------------------------------------------------------------------------

def _foreign(names, epoch, valid_stems):
    # names are made printable (undecodable bytes replaced) so that they can be returned to R
    return sorted({str(n).encode('utf-8', 'replace').decode('utf-8').replace('\x00', '?') for n in names if _stem(n) not in valid_stems})


def _shots(path):
    return [s for rec in _read_json(path) for s in rec.get('shots', {})]


def _file_contains(path, patterns, chunk=1 << 24):
    """Patterns (bytes) found in a (possibly large, binary) file."""
    found, overlap, prev = set(), max(len(p) for p in patterns), b''
    with open(path, 'rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            data = prev + block
            found.update(p for p in patterns if p in data)
            prev = data[-overlap:]
    return found


def verify_epoch_project(workdir, epoch, valid_stems, all_epochs):
    """
    Checks that an epoch project only refers to images of this epoch.
    'critical': files that decide which images are used to build the point cloud, the mesh texture and the
    orthomosaic. 'other': files that only change the processing (sparse points, GSD estimate, report).
    """
    root = os.path.join(workdir, epoch)
    und = os.path.join(root, 'opensfm', 'undistorted')
    res = {'epoch': epoch, 'critical': {}, 'other': {}, 'checked': []}

    def check(level, label, names):
        res['checked'].append(label)
        bad = _foreign(names, epoch, valid_stems)
        if bad:
            res[level][label] = bad[:10] + ([f'... ({len(bad)} in total)'] if len(bad) > 10 else [])

    if os.path.isdir(os.path.join(root, 'images')):
        check('critical', 'images/', os.listdir(os.path.join(root, 'images')))
    if os.path.isdir(os.path.join(und, 'images')):
        check('critical', 'opensfm/undistorted/images/', os.listdir(os.path.join(und, 'images')))
    if os.path.isfile(os.path.join(und, 'reconstruction.json')):
        check('critical', 'opensfm/undistorted/reconstruction.json', _shots(os.path.join(und, 'reconstruction.json')))
    nvm = os.path.join(und, 'reconstruction.nvm')
    if os.path.isfile(nvm):
        with open(nvm, **ENC) as f:
            lines = f.read().split('\n')
        i = next(k for k, line in enumerate(lines) if k > 0 and line.strip())
        check('critical', 'opensfm/undistorted/reconstruction.nvm (texturing)',
              [line.split()[0] for line in lines[i + 1:i + 1 + int(lines[i].split()[0])] if line.strip()])
    others = [e for e in all_epochs if e != epoch]
    for mvs in ('scene.mvs', 'scene_dense.mvs'):
        path = os.path.join(root, 'opensfm', 'undistorted', 'openmvs', mvs)
        if not os.path.isfile(path):
            path = os.path.join(root, 'openmvs', mvs)
        if os.path.isfile(path) and others:
            res['checked'].append(f'openmvs/{mvs}')
            hits = _file_contains(path, [(e + SEP).encode() for e in others])
            if hits:
                res['critical'][f'openmvs/{mvs}'] = sorted(h.decode() for h in hits)
    if os.path.isfile(os.path.join(root, 'opensfm', 'reconstruction.json')):
        check('other', 'opensfm/reconstruction.json', _shots(os.path.join(root, 'opensfm', 'reconstruction.json')))
    if os.path.isfile(os.path.join(root, 'images.json')):
        check('other', 'images.json', [p.get('filename', '') for p in _read_json(os.path.join(root, 'images.json'))])
    tracks = os.path.join(und, 'tracks.csv')
    if os.path.isfile(tracks):
        check('other', 'opensfm/undistorted/tracks.csv', {image for image, _, _, _ in iter_tracks(tracks)})
    geo = os.path.join(root, 'odm_report', 'shots.geojson')
    if os.path.isfile(geo):
        check('other', 'odm_report/shots.geojson',
              [ft.get('properties', {}).get('filename', '') for ft in _read_json(geo).get('features', [])])
    res['ok'] = not res['critical']
    return res


def verify_projects(workdir, verbose=True):
    """
    Checks all the epoch projects of a Time-SIFT working folder (out_dir_project). Can be run on a finished
    or interrupted run. Returns a list of results (one per epoch); 'ok' is False if an image of another
    epoch was found in a critical file.
    """
    workdir = os.path.abspath(workdir)
    epochs = read_images_epochs(workdir)
    results = []
    for epoch, stems in epochs.items():
        if not os.path.isdir(os.path.join(workdir, epoch)):
            continue
        r = verify_epoch_project(workdir, epoch, stems, list(epochs))
        results.append(r)
        if verbose:
            state = 'OK' if r['ok'] else 'PROBLEM'
            log(f'Check of epoch {epoch}: {state} ({len(r["checked"])} files checked)')
            for level in ('critical', 'other'):
                for label, names in r[level].items():
                    log(f'    {level}: {label} contains images of other epochs: {", ".join(names[:3])}')
    with open(os.path.join(workdir, 'epochs_check.json'), 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=1)          # ensure_ascii: undecodable bytes written as escapes
    return results


# ---------------------------------------------------------------------------------------------
# 5. Colour calibration (equivalent of Metashape calibrateColors on tie points)
# ---------------------------------------------------------------------------------------------

def compute_color_gains(block_dir, max_obs=3000000, seed=0):
    """
    One gain per image and per channel, so that the tie points of the whole multi-temporal block have
    the same colour in all the images where they are seen (images of all dates are calibrated together,
    as in the Metashape pipeline where calibrateColors() is run on the merged Time-SIFT chunk).

    Model: log(colour observed) = log(gain of the image) + log(colour of the tie point), fitted by
    alternating least squares with removal of outliers; the mean of the log gains is 0 for each channel
    (the overall brightness and colour balance of the block are kept).
    Tie point colours come from OpenSfM tracks.csv (colour of the image at the feature location).
    Returns {image stem: (gain_r, gain_g, gain_b)}; the gain multiplies the pixel values.
    """
    import numpy as np

    recs = _read_json(os.path.join(block_dir, 'opensfm', 'reconstruction.json'))
    points = {str(p) for rec in recs for p in rec.get('points', {})}
    shots = {s for rec in recs for s in rec.get('shots', {})}
    img_index, trk_index = {}, {}
    rows_i, rows_t, cols = [], [], []
    for image, track, rgb, _ in iter_tracks(os.path.join(block_dir, 'opensfm', 'tracks.csv')):
        if track not in points or image not in shots:
            continue
        if min(rgb) < 5 or max(rgb) > 250:              # dark or saturated
            continue
        rows_i.append(img_index.setdefault(image, len(img_index)))
        rows_t.append(trk_index.setdefault(track, len(trk_index)))
        cols.append(tuple(float(c) for c in rgb))
    if not cols:
        raise TimeSiftError('Colour calibration: no tie point colour found in tracks.csv')
    I, T, L = np.array(rows_i), np.array(rows_t), np.log(np.array(cols))
    # tie points seen in at least 2 images
    keep = np.bincount(T)[T] >= 2
    if keep.sum() > max_obs:
        rng = np.random.default_rng(seed)
        tracks_kept = rng.choice(len(trk_index), size=max(1, int(len(trk_index) * max_obs / keep.sum())), replace=False)
        mask = np.zeros(len(trk_index), bool)
        mask[tracks_kept] = True
        keep &= mask[T]
    I, T, L = I[keep], T[keep], L[keep]
    _, T = np.unique(T, return_inverse=True)
    n_img, n_trk = len(img_index), T.max() + 1
    a = np.zeros((n_img, 3))
    w = np.ones(len(I), bool)
    for it in range(30):
        cnt_t = np.maximum(np.bincount(T, w, n_trk), 1)
        t = np.stack([np.bincount(T, w * (L[:, c] - a[I, c]), n_trk) for c in range(3)], 1) / cnt_t[:, None]
        cnt_i = np.maximum(np.bincount(I, w, n_img), 1)
        a = np.stack([np.bincount(I, w * (L[:, c] - t[T, c]), n_img) for c in range(3)], 1) / cnt_i[:, None]
        a -= a.mean(0)
        if it in (9, 19):                               # removal of outliers (3.5 robust sd)
            r = np.abs(L - a[I] - t[T]).max(1)
            w = r < 3.5 * 1.4826 * np.median(r[w]) + 1e-6
    gains = np.clip(np.exp(-a), 0.5, 2.0)
    n_obs = np.bincount(I, w, n_img)
    names = sorted(img_index, key=img_index.get)
    return {_stem(n): tuple(float(g) for g in gains[k]) for k, n in enumerate(names)}, \
        {_stem(n): int(n_obs[k]) for k, n in enumerate(names)}


def save_color_gains(path, gains, n_obs):
    with open(path, 'w', encoding='utf-8') as f:
        f.write('image,epoch,gain_r,gain_g,gain_b,n_obs\n')
        for k in sorted(gains):
            g = gains[k]
            f.write(f'{k},{epoch_of(k)},{g[0]:.4f},{g[1]:.4f},{g[2]:.4f},{n_obs.get(k, 0)}\n')


def write_tiff(path, arr):
    """
    Writes an image (H x W x C uint8 / uint16 array) as an uncompressed baseline TIFF, with the SampleFormat
    tag: the texturing step of ODM (texrecon, MVE load_tiff_file_headers) reads this tag without default value
    and refuses the file when it is missing ("Cannot determine image format"), which is the case of the TIFF
    files written by Pillow. Python standard library only.
    """
    if arr.ndim == 2:
        arr = arr[:, :, None]
    h, w, c = arr.shape
    bits = arr.dtype.itemsize * 8
    data = arr.astype(arr.dtype.newbyteorder('<'), copy=False).tobytes()
    if len(data) >= 2 ** 32 - 4096:
        raise TimeSiftError(f'Image too large for a classic TIFF: {path}')
    entries = []                                     # (tag, type, count, values); SHORT = 3, LONG = 4
    extra = b''
    n_entries = 11 + (1 if c in (2, 4) else 0)
    ifd_size = 2 + 12 * n_entries + 4
    extra_offset = 8 + ifd_size

    def short_array(values):
        nonlocal extra
        if len(values) <= 2:
            return values
        off = extra_offset + len(extra)
        extra += struct.pack('<%dH' % len(values), *values)
        return off

    bps = short_array([bits] * c)
    sfm = short_array([1] * c)                       # SampleFormat: unsigned integer
    data_offset = extra_offset + len(extra)
    entries = [(256, 4, 1, [w]), (257, 4, 1, [h]), (258, 3, c, bps), (259, 3, 1, [1]),
               (262, 3, 1, [2 if c >= 3 else 1]), (273, 4, 1, [data_offset]), (277, 3, 1, [c]),
               (278, 4, 1, [h]), (279, 4, 1, [len(data)]), (284, 3, 1, [1])]
    if c in (2, 4):
        entries.append((338, 3, 1, [2]))             # ExtraSamples: unassociated alpha
    entries.append((339, 3, c, sfm))
    ifd = struct.pack('<H', len(entries))
    for tag, typ, count, val in entries:
        if isinstance(val, int):                     # offset to an array
            ifd += struct.pack('<HHII', tag, typ, count, val)
        elif typ == 3:
            v = list(val) + [0] * (2 - len(val))
            ifd += struct.pack('<HHIHH', tag, typ, count, v[0], v[1])
        else:
            ifd += struct.pack('<HHII', tag, typ, count, val[0])
    ifd += struct.pack('<I', 0)
    with open(path, 'wb') as f:
        f.write(b'II*\x00' + struct.pack('<I', 8) + ifd + extra + data)


def apply_gain(src, dst, gain):
    """Writes a colour-corrected copy of an image (the source, possibly hard-linked, is not modified)."""
    import numpy as np
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(src) as im:
            arr = np.asarray(im)
    except Exception:
        from osgeo import gdal
        ds = gdal.Open(src)
        arr = ds.ReadAsArray()
        arr = np.moveaxis(arr, 0, -1) if arr.ndim == 3 else arr
        ds = None
    out = arr.astype(np.float32)
    if out.ndim == 3:
        for c in range(min(3, out.shape[-1])):
            out[..., c] *= gain[c]
    vmax = np.iinfo(arr.dtype).max if np.issubdtype(arr.dtype, np.integer) else None
    if vmax is not None:
        out = np.clip(np.rint(out), 0, vmax)
    out = out.astype(arr.dtype)
    if os.path.exists(dst):
        os.remove(dst)
    write_tiff(dst, out)


# ---------------------------------------------------------------------------------------------
# 6. Complete process
# ---------------------------------------------------------------------------------------------

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
                         calibrate_col=True,
                         check_dates=True,
                         odm_path=None,
                         odm_runner="native",
                         odm_args=None,
                         overwrite=False,
                         keep_project=None,
                         tmp_dir=None,
                         ):
    """
    Complete Time-SIFT process with ODM: one orthomosaic (and optionally one DSM) per date or per flight.

    :param str pathDIR: folder with one sub-folder per flight (date as YYYYMMDD or YYYYMM in the folder name).
    :param str out_dir_ortho: folder where the orthomosaics are saved.
    :param str out_dir_DEM: folder where the DSMs are saved (None: no DSM).
    :param str out_dir_project: folder of the ODM projects (working directory). Default: <out_dir_ortho>/_odm_timesift_.
        It should be on the same disk as the images (images are then hard-linked instead of copied).
    :param float resol_ref: resolution (m) of the orthomosaics and DSMs.
    :param int downscale_factor_alignement, downscale_factor_depth_map: Metashape values, converted to ODM levels.
    :param bool calibrate_col: colour calibration of the images on the tie points of the whole block.
    :param bool check_dates: reports the EXIF days of the images of each folder (information only, no error).
    :param str odm_path: native: path of run.bat / run.sh ; docker: image name ; apptainer: image file.
    :param str odm_runner: 'native', 'docker' or 'apptainer'.
    :param list odm_args: additional ODM options, e.g. ['--min-num-features', '20000'].
    :param bool overwrite: deletes existing ODM projects (block and epochs) before running. If False, a previous
        run with the same images and parameters is resumed (an error is raised if they differ).
    :param bool keep_project: keeps the ODM projects at the end (default: True if out_dir_project is given).
    :param str tmp_dir: directory for the temporary files of ODM (default: the system one).
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
    if os.path.commonpath([workdir, os.path.abspath(pathDIR)]) == os.path.abspath(pathDIR):
        raise TimeSiftError('out_dir_project must not be inside path_in')
    if overwrite and os.path.isdir(workdir):
        for e in os.listdir(workdir):                  # everything except the folder of temporary files
            if e == TMP_NAME:
                continue
            path = os.path.join(workdir, e)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    os.makedirs(workdir, exist_ok=True)
    if isinstance(odm_args, str):
        odm_args = odm_args.split()
    if int(downscale_factor_alignement) not in ALIGN_QUALITY or int(downscale_factor_depth_map) not in DEPTH_QUALITY:
        raise ValueError('downscale_factor_alignement must be in 0, 1, 2, 4, 8 and downscale_factor_depth_map in 1, 2, 4, 8, 16')

    options = ['--orthophoto-resolution', str(float(resol_ref) * 100),
               '--feature-quality', ALIGN_QUALITY[int(downscale_factor_alignement)],
               '--pc-quality', DEPTH_QUALITY[int(downscale_factor_depth_map)],
               '--orthophoto-compression', 'LZW',
               '--skip-3dmodel', '--skip-report']
    if out_dir_DEM is not None:
        options += ['--dsm', '--dem-resolution', str(float(resol_ref) * 100)]
    options += [str(a) for a in (odm_args or [])]

    if site_name:                                      # same file names as the Metashape pipeline: <site>_<date><suffix>
        site_name = site_name + '_'
    epochs = list_epochs(pathDIR, group_by_flight=group_by_flight, folder_names=folder_names, check_dates=check_dates)
    log(f'{len(epochs)} epochs: ' + ', '.join(f'{e} ({len(i)} images)' for e, i in epochs.items()))

    # a previous run in workdir is only resumed if it was made with the same images and parameters
    params = {'images': {e: sorted(n for _, n in i) for e, i in epochs.items()},
              'options': options, 'calibrate_col': bool(calibrate_col)}
    params_path = os.path.join(workdir, PARAMS_FILE)
    if [e for e in os.listdir(workdir) if e != TMP_NAME]:
        old = None
        if os.path.isfile(params_path):
            with open(params_path, encoding='utf-8') as f:
                old = json.load(f)
        if old != params:
            raise TimeSiftError(f'{workdir} contains a previous run made with other images or parameters. '
                                f'Use overwrite = TRUE to delete it, or give another out_dir_project.')
    with open(params_path, 'w', encoding='utf-8') as f:
        json.dump(params, f, indent=1)

    # 1. SfM on the multi-temporal block
    block_dir = os.path.join(workdir, BLOCK_NAME)
    sfm_done = os.path.join(block_dir, 'opensfm', 'undistorted', 'reconstruction.json')
    if os.path.isfile(sfm_done):
        log('Existing SfM block found: it is re-used (use overwrite=True to recompute it)')
    else:
        prepare_block(workdir, epochs)
        code = run_odm(odm_runner, odm_path, workdir, BLOCK_NAME, options + ['--end-with', 'opensfm'], tmp_dir)
        if not os.path.isfile(sfm_done):
            raise TimeSiftError(f'ODM failed during the SfM of the multi-temporal block (exit code {code}). '
                                f'See {os.path.join(workdir, "odm_" + BLOCK_NAME + ".log")}')
    t1 = time.time()
    log(f'SfM on the multi-temporal block done ({t1 - t0:.0f} s)')
    all_shots = set(_stem(s) for s in _shots(sfm_done))
    for epoch, images in epochs.items():
        n = sum(_stem(nm) in all_shots for _, nm in images)
        log(f'  epoch {epoch}: {n} / {len(images)} images aligned')

    # 2. colour calibration on the tie points of the whole block
    gains = None
    if calibrate_col:
        gains, n_obs = compute_color_gains(block_dir)
        save_color_gains(os.path.join(workdir, COLOR_FILE), gains, n_obs)
        g = list(gains.values())
        log(f'Colour calibration: {len(g)} images, gains from {min(min(x) for x in g):.2f} to '
            f'{max(max(x) for x in g):.2f} (details in {COLOR_FILE})')

    # 3. dense matching, DSM and orthomosaic for each epoch
    results = {}
    for epoch, images in epochs.items():
        te = time.time()
        valid = {_stem(n) for _, n in images}
        ortho = os.path.join(workdir, epoch, 'odm_orthophoto', 'odm_orthophoto.tif')
        if os.path.isfile(ortho):
            log(f'Epoch {epoch}: orthomosaic already computed, skipped')
        else:
            if os.path.isdir(os.path.join(workdir, epoch)):
                shutil.rmtree(os.path.join(workdir, epoch))        # unfinished previous run
            make_epoch_project(workdir, epoch, images, gains)
            code = run_odm(odm_runner, odm_path, workdir, epoch, options + ['--rerun-from', 'openmvs'], tmp_dir)
            if not os.path.isfile(ortho):
                raise TimeSiftError(f'ODM failed for epoch {epoch} (exit code {code}). '
                                    f'See {os.path.join(workdir, "odm_" + epoch + ".log")}')
        check = verify_epoch_project(workdir, epoch, valid, list(epochs))
        if not check['ok'] or check['other']:
            raise TimeSiftError(f'Epoch {epoch}: images of other epochs found in the project: '
                                f'{json.dumps(check["critical"] or check["other"])}')
        log(f'Epoch {epoch}: checked, only images of this epoch in the {len(check["checked"])} files that list images')
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

    verify_projects(workdir, verbose=False)
    timings_summary(workdir)
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
    p.add_argument('--no_calibrate_col', action='store_true')
    p.add_argument('--no_check_dates', action='store_true')
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
                         calibrate_col=not args.no_calibrate_col, check_dates=not args.no_check_dates,
                         odm_path=args.odm_path, odm_runner=args.odm_runner, odm_args=odm_args,
                         overwrite=args.overwrite, keep_project=True if args.keep_project else None)


# reticulate::source_python() also runs this file as '__main__': the command line is only parsed when it is used
if __name__ == '__main__' and any(a.startswith('--pathDIR') for a in sys.argv[1:]):
    main()
