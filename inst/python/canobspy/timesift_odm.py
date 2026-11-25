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
#   - optional colour calibration of the images on the tie points of the whole multi-temporal block or of each
#     date (gains per image and channel, vignetting per camera and channel: equivalent of Metashape
#     calibrateColors(white_balance=True), used by the Metashape pipeline of canobsR);
#   - ODM options set to reproduce the Metashape pipeline of canobsR (keypoints, GPS accuracy, resolution, no crop);
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

try:
    from .flights import epoch_of_folder
except ImportError:                                  # run as a script
    from flights import epoch_of_folder

IMG_EXT = ('.jpg', '.jpeg', '.png', '.tif', '.tiff')
DATE_PATTERN = r'\d{4}(0[1-9]|1[0-2])([0-2][0-9]|3[01])?'
BLOCK_NAME = 'timesift_block'
SEP = '__'                      # separator between the epoch and the original image name
RESERVED = {BLOCK_NAME, 'time-sift-block'}
PARAMS_FILE = 'canobs_timesift_params.json'
EPOCH_PARAMS_FILE = 'canobs_epoch_params.json'   # parameters used for the products of an epoch project
# ODM options used after the SfM only: number of values and first ODM stage that uses them. A change of these options
# does not require a new SfM: each epoch is run again from this stage. Any other option is considered as an SfM
# option (a change requires a new run, overwrite = TRUE).
EPOCH_STAGES = ['openmvs', 'odm_filterpoints', 'odm_meshing', 'mvs_texturing']
DENSE_OPTIONS = {'--pc-quality': (1, 'openmvs'), '--pc-skip-geometric': (0, 'openmvs'),
                 '--pc-filter': (1, 'odm_filterpoints'), '--pc-sample': (1, 'odm_filterpoints'),
                 '--mesh-size': (1, 'odm_meshing'), '--mesh-octree-depth': (1, 'odm_meshing'),
                 '--use-3dmesh': (0, 'odm_meshing'), '--skip-3dmodel': (0, 'odm_meshing'),
                 '--texturing-skip-global-seam-leveling': (0, 'mvs_texturing'),
                 '--texturing-keep-unseen-faces': (0, 'mvs_texturing'),
                 '--texturing-single-material': (0, 'mvs_texturing'),
                 '--crop': (1, 'mvs_texturing'), '--dsm': (0, 'mvs_texturing'), '--dtm': (0, 'mvs_texturing'),
                 '--dem-resolution': (1, 'mvs_texturing'), '--dem-gapfill-steps': (1, 'mvs_texturing'),
                 '--dem-decimation': (1, 'mvs_texturing'), '--orthophoto-compression': (1, 'mvs_texturing'),
                 '--orthophoto-cutline': (0, 'mvs_texturing'), '--skip-report': (0, 'mvs_texturing'),
                 '--build-overviews': (0, 'mvs_texturing'), '--cog': (0, 'mvs_texturing')}
# Options that change how ODM runs, not its results: never compared between runs (a run can be resumed with them)
EXEC_OPTIONS = {'--no-gpu': 0, '--max-concurrency': 1, '--optimize-disk-space': 0}
# Settings of the Metashape pipeline of canobsR, reproduced with ODM (see the documentation of the engines):
GPS_ACCURACY = 10               # m, Metashape default accuracy of the camera positions (Reference settings)
KEYPOINT_LIMIT = 10000          # keypoints per image (Metashape pipeline: 100000; see ENGINES.md, tests of 24/11/2026)
TIMINGS_FILE = 'timings.csv'
TMP_NAME = '_tmp'               # default folder of the temporary files, created by generate_Mosa() in out_dir_project
COLOR_FILE = 'color_gains.csv'

# Metashape downscale factors -> ODM quality levels, at the same image resolution:
# alignment: Metashape 1 (High) = original images; ODM feature-quality ultra = original images (1),
#            high = 1/2, medium = 1/4, low = 1/8, lowest = 1/16; ODM never extracts the features on images larger
#            than 4480 px (hard-coded in opendm/osfm.py): 5472 px images are reduced to 4480 px
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


def _text(s):
    """Printable text: bytes of an ODM file that are not UTF-8 (kept as surrogates) are replaced by '?'."""
    return str(s).encode('utf-8', 'surrogateescape').decode('utf-8', 'replace').replace('\ufffd', '?')


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
    """
    Yields (image, track id, (r, g, b), raw bytes of the observation, (x, y)) for both formats. x, y are the
    normalized coordinates of the feature in the (distorted) image: (pixel + 0.5 - size / 2) / max(width, height).
    """
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
                    yield image, track, rec[4:7], mm[start:pos], rec[1:3]
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
                    xy = (float(cells[3]), float(cells[4]))
                except ValueError:
                    rgb, xy = (0, 0, 0), (0.0, 0.0)
                yield cells[0].decode('utf-8', 'replace'), cells[1].decode('utf-8', 'replace'), rgb, line, xy


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


def exif_latlon(path):
    """(latitude, longitude) in decimal degrees from the EXIF GPS tags, None if absent."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            g = im.getexif().get_ifd(0x8825)
    except Exception:
        return None
    if not g or 2 not in g or 4 not in g:
        return None

    def deg(v, ref):
        d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
        return -d if str(ref).strip('\x00 ').upper() in ('S', 'W') else d
    try:
        return deg(g[2], g.get(1, 'N')), deg(g[4], g.get(3, 'E'))
    except Exception:
        return None


def utm_epsg(lat, lon):
    """EPSG code of the WGS 84 / UTM zone of a point (with the exceptions of Norway and Svalbard)."""
    zone = int((lon + 180) // 6) % 60 + 1
    if 56 <= lat < 64 and 3 <= lon < 12:
        zone = 32
    if 72 <= lat < 84 and 0 <= lon < 42:
        zone = 31 if lon < 9 else 33 if lon < 21 else 35 if lon < 33 else 37
    return (32600 if lat >= 0 else 32700) + zone, zone


def utm_from_images(path_in, folder_names=None, per_folder=3):
    """
    UTM zone (WGS 84) of a site, from the GPS positions of a few images of each flight folder (the first ones
    with GPS tags): median latitude and longitude. Returns a dict: epsg, zone, hemisphere, lat, lon, n_images,
    zones (all the zones met: more than one if the site is on a zone boundary).
    """
    folders = folder_names or sorted(d for d in os.listdir(path_in) if os.path.isdir(os.path.join(path_in, d)))
    pts = []
    for d in folders:
        k = 0
        for root, _, files in os.walk(os.path.join(path_in, d)):
            for n in sorted(files):
                if os.path.splitext(n)[1].lower() not in ('.jpg', '.jpeg', '.tif', '.tiff', '.dng'):
                    continue
                ll = exif_latlon(os.path.join(root, n))
                if ll:
                    pts.append(ll)
                    k += 1
                if k >= per_folder:
                    break
            if k >= per_folder:
                break
    if not pts:
        raise ValueError(f'No image with GPS tags in {path_in}: give the crs (e.g. "EPSG:32633").')
    lats = sorted(p[0] for p in pts)
    lons = sorted(p[1] for p in pts)
    lat, lon = lats[len(lats) // 2], lons[len(lons) // 2]
    epsg, zone = utm_epsg(lat, lon)
    return {'epsg': epsg, 'zone': zone, 'hemisphere': 'N' if lat >= 0 else 'S', 'lat': round(lat, 5),
            'lon': round(lon, 5), 'n_images': len(pts), 'zones': sorted({utm_epsg(a, b)[0] for a, b in pts})}


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
            epoch = epoch_of_folder(flight)          # date, plus the drone if the folder names one
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


def check_odm_gpu(odm_path, fix=True):
    """
    Native ODM on Windows: the installer puts a dummy nvcuda.dll (a few KB) next to OpenMVS, in
    SuperBuild/install/bin, so that OpenMVS starts on PCs without an NVIDIA card. Windows loads it instead of the
    NVIDIA driver (C:/Windows/System32/nvcuda.dll): OpenMVS cannot start CUDA, fails, and ODM runs it again on the
    CPU without saying so. Measured on verzy (Quadro RTX 6000): dense matching 3.7 x faster without the dummy DLL.

    When the NVIDIA driver is installed and fix=True, the dummy DLL is renamed nvcuda.dll.odm_dummy (to undo: rename
    it back). Every ODM installation or update puts it back: this check runs before each Time-SIFT run.
    Returns a dict: status = 'not_windows' | 'no_dummy' | 'no_nvidia_driver' | 'renamed' | 'dummy_present', path.
    """
    if os.name != 'nt' or not odm_path:
        return {'status': 'not_windows', 'path': ''}
    dummy = os.path.join(os.path.dirname(os.path.abspath(odm_path)), 'SuperBuild', 'install', 'bin', 'nvcuda.dll')
    real = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'nvcuda.dll')
    if not (os.path.isfile(dummy) and os.path.getsize(dummy) < 1e6):
        return {'status': 'no_dummy', 'path': dummy}
    if not os.path.isfile(real):
        return {'status': 'no_nvidia_driver', 'path': dummy}            # the dummy is needed: kept
    if fix:
        try:
            target = dummy + '.odm_dummy'
            if os.path.exists(target):
                os.remove(target)
            os.rename(dummy, target)
            log('*' * 100)
            log(f'GPU: the dummy {dummy} (installed by ODM) hid the NVIDIA driver and forced OpenMVS on the CPU.')
            log(f'GPU: it has been renamed {os.path.basename(target)}: OpenMVS now uses the GPU.')
            log('*' * 100)
            return {'status': 'renamed', 'path': target}
        except OSError as e:
            log(f'GPU: the dummy DLL could not be renamed ({e})')
    log('!' * 100)
    log('!!! ODM WILL NOT USE THE GPU (dense matching 3-4 x slower).')
    log(f'!!! The dummy {dummy} (installed by ODM) hides the NVIDIA driver.')
    log('!!! Rename it nvcuda.dll.odm_dummy (administrator rights on the ODM folder), or run R as administrator.')
    log('!' * 100)
    return {'status': 'dummy_present', 'path': dummy}


LAST_RUN = {'densify_failed': False}     # set by run_odm(): the dense matching (OpenMVS) failed in the last run


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
    LAST_RUN['densify_failed'] = False
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
            if 'ran out of memory' in line or re.search(r'stages[\\/]openmvs\.py', line):
                LAST_RUN['densify_failed'] = True
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
        for image, track, _, raw, _ in iter_tracks(path):
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


def retexture_epoch(workdir, epoch, images, gains=None):
    """
    Replaces the undistorted images of an epoch project by new ones (colour-corrected with gains, or the original
    ones), and deletes the texture and the orthomosaic, before ODM is run again from a stage after the dense
    matching (point filtering, meshing or texturing): the alignment and the dense matching are kept.
    """
    block = os.path.join(workdir, BLOCK_NAME)
    dst = os.path.join(workdir, epoch)
    valid = {_stem(name) for _, name in images}
    und_src = os.path.join(block, 'opensfm', 'undistorted', 'images')
    und_dst = os.path.join(dst, 'opensfm', 'undistorted', 'images')
    for f in os.listdir(und_src):
        if _stem(f) in valid:
            d = os.path.join(und_dst, f)
            if os.path.exists(d):
                os.remove(d)                             # (a hard link: the block image is not modified)
            if gains is not None and _stem(f) in gains:
                apply_gain(os.path.join(und_src, f), d, gains[_stem(f)])
            else:
                _link_or_copy(os.path.join(und_src, f), d)
    for sub in ('odm_texturing', 'odm_texturing_25d', 'odm_orthophoto'):
        if os.path.isdir(os.path.join(dst, sub)):
            shutil.rmtree(os.path.join(dst, sub))
    if os.path.isfile(os.path.join(dst, EPOCH_PARAMS_FILE)):
        os.remove(os.path.join(dst, EPOCH_PARAMS_FILE))


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
        check('other', 'opensfm/undistorted/tracks.csv', {obs[0] for obs in iter_tracks(tracks)})
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

# Vignetting: as in Metashape (camera vignetting modelled by a 3-degree bivariate polynomial), one polynomial per
# camera (sensor) and per channel, without constant term (no correction at the image centre). It is fitted in log
# space on the normalized coordinates of the distorted image, scaled to [-1, 1] along the longest side.
VIGN_TERMS = 9


def _vign_terms(u, v):
    """Monomials u^i v^j with 1 <= i + j <= 3 (last axis)."""
    import numpy as np
    return np.stack([u, v, u * u, u * v, v * v, u * u * u, u * u * v, u * v * v, v * v * v], -1)


def _fit_gains(I, T, L, n_img, P=None, C=None, n_cam=0, n_iter=60):
    """
    Alternating least squares, independently for each channel, in log space:
        log(colour observed) = a[image] + t[tie point] + V[camera](x, y)
    V (vignetting, optional: P = monomials of the observations, C = camera of the observations) is a 3-degree
    polynomial without constant. Outliers (residual > 3.5 robust sd) are removed twice. The mean of a over the
    images is 0 for each channel (the overall colour level is not changed).
    Returns (a, beta, weights of the observations kept); beta: (n_cam, VIGN_TERMS, 3) or None.
    """
    import numpy as np
    _, T = np.unique(T, return_inverse=True)
    n_trk = T.max() + 1
    a = np.zeros((n_img, 3))
    V = np.zeros_like(L)
    beta = np.zeros((n_cam, VIGN_TERMS, 3)) if P is not None else None
    w = np.ones(len(I), bool)
    seen = np.bincount(I, minlength=n_img) > 0
    for it in range(n_iter):
        R = L - V
        cnt_t = np.maximum(np.bincount(T, w, n_trk), 1)
        t = np.stack([np.bincount(T, w * (R[:, c] - a[I, c]), n_trk) for c in range(3)], 1) / cnt_t[:, None]
        cnt_i = np.maximum(np.bincount(I, w, n_img), 1)
        a_new = np.stack([np.bincount(I, w * (R[:, c] - t[T, c]), n_img) for c in range(3)], 1) / cnt_i[:, None]
        a_new[seen] -= a_new[seen].mean(0)
        delta = np.abs(a_new - a).max()
        a = a_new
        if P is not None:
            Rv = L - a[I] - t[T]
            for k in range(n_cam):
                m = (C == k) & w
                if m.sum() < 100 * VIGN_TERMS:
                    continue
                Pk = P[m].astype(np.float64)
                A = Pk.T @ Pk
                A += np.eye(VIGN_TERMS) * 1e-9 * np.trace(A)
                beta[k] = np.linalg.solve(A, Pk.T @ Rv[m])
            for k in range(n_cam):
                m = C == k
                V[m] = P[m] @ beta[k]
        if it in (14, 29):                              # removal of outliers (3.5 robust sd)
            r = np.abs(L - a[I] - t[T] - V).max(1)
            w = r < 3.5 * 1.4826 * np.median(r[w]) + 1e-6
        elif it > 30 and delta < 1e-6:
            break
    return a, beta, w


def _track_sampling(path, max_obs, n_probe=5000):
    """Fraction of the tie points to keep so that about max_obs observations are read (estimated from the file size)."""
    size = os.path.getsize(path)
    n, total = 0, 0
    for obs in iter_tracks(path):
        total += len(obs[3])
        n += 1
        if n >= n_probe:
            break
    if n == 0:
        return 1.0
    return min(1.0, max_obs / (size / (total / n)))


def compute_color_gains(block_dir, by='chunk', vignetting=True, max_obs=3000000):
    """
    Colour calibration of the images on the tie points of the multi-temporal block (equivalent of Metashape
    calibrateColors(TiePointsData, white_balance=True), run by the Metashape pipeline of canobsR): one gain per image
    and per channel (white_balance=True: each channel corrected separately) and, with vignetting=True, one
    vignetting polynomial per camera and per channel, so that a tie point has the same colour in all the images
    where it is seen.

    by = 'chunk': all the images of the multi-temporal block are calibrated together (as calibrateColors() run on the
           merged Time-SIFT chunk): colours are harmonised inside each mosaic and between mosaics; the mean of the
           log gains over all the images is 0.
    by = 'date': the same tie points and observations, but each date (epoch) is calibrated alone (as
           calibrateColors() run on each date chunk after the split): a tie point may have a different colour at
           each date; colours are harmonised inside each mosaic, and the colour level of each date is kept (mean of
           the log gains over the images of the date = 0). The vignetting is then estimated for each date.
    Observations that carry no constraint (a tie point seen in only one image of the group) are not used.

    Model: log(colour observed) = log(gain of the image) + log(colour of the tie point) + vignetting(x, y), fitted by
    alternating least squares with removal of outliers. Tie point colours and positions come from OpenSfM tracks.csv
    (colour of the image at the feature location, normalized coordinates of the feature); dark (< 5) or saturated
    (> 250) observations are not used; above max_obs observations, a random subset of tie points is used.

    Returns (gains, n_obs, vign, models):
      gains  {image stem: (gain_r, gain_g, gain_b)}, the gain multiplies the pixel values (clipped to 0.5-2);
      n_obs  {image stem: number of observations used};
      vign   {image stem: VIGN_TERMS x 3 coefficients (list) or None}, see vignetting_factor();
      models list of (group, camera, coefficients) for the report.
    """
    import numpy as np
    import zlib
    from array import array
    if by not in ('chunk', 'date'):
        raise ValueError("calibrate_col_by must be 'chunk' or 'date'")

    recs = _read_json(os.path.join(block_dir, 'opensfm', 'reconstruction.json'))
    points = {str(p) for rec in recs for p in rec.get('points', {})}
    shot_cam = {s: v.get('camera', '') for rec in recs for s, v in rec.get('shots', {}).items()}
    tracks_path = os.path.join(block_dir, 'opensfm', 'tracks.csv')
    keep_p = _track_sampling(tracks_path, max_obs)
    threshold = int(keep_p * 0xFFFFFFFF)
    img_index, trk_index = {}, {}
    rows_i, rows_t, cols, xy = array('i'), array('i'), array('B'), array('f')
    for image, track, rgb, _, pos in iter_tracks(tracks_path):
        if track not in points or image not in shot_cam:
            continue
        if keep_p < 1 and zlib.crc32(track.encode()) > threshold:
            continue
        if min(rgb) < 5 or max(rgb) > 250:              # dark or saturated
            continue
        rows_i.append(img_index.setdefault(image, len(img_index)))
        rows_t.append(trk_index.setdefault(track, len(trk_index)))
        cols.extend(rgb)
        xy.extend(pos)
    if not cols:
        raise TimeSiftError('Colour calibration: no tie point colour found in tracks.csv')
    names = sorted(img_index, key=img_index.get)
    I = np.frombuffer(rows_i, np.int32).astype(np.int64)
    T = np.frombuffer(rows_t, np.int32).astype(np.int64)
    L = np.log(np.frombuffer(cols, np.uint8).reshape(-1, 3).astype(np.float64))
    XY = np.frombuffer(xy, np.float32).reshape(-1, 2) * 2       # [-1, 1] along the longest side
    cam_names = sorted(set(shot_cam[n] for n in names))
    img_cam = np.array([cam_names.index(shot_cam[n]) for n in names])

    if by == 'chunk':
        groups = [('all', np.ones(len(I), bool))]
    else:
        img_epoch = np.array([epoch_of(n) for n in names])
        obs_epoch = img_epoch[I]
        groups = [(e, obs_epoch == e) for e in np.unique(img_epoch)]

    a = np.zeros((len(names), 3))
    n_obs = np.zeros(len(names))
    vign = {}
    models = []
    for label, g in groups:
        Ig, Tg, Lg = I[g], T[g], L[g]
        _, inv, cnt = np.unique(Tg, return_inverse=True, return_counts=True)
        keep = cnt[inv] >= 2                             # tie points seen in at least 2 images of the group
        if keep.sum() == 0:
            continue
        Ig, Tg, Lg = Ig[keep], Tg[keep], Lg[keep]
        P = C = None
        if vignetting:
            XYg = XY[g][keep]
            P = _vign_terms(XYg[:, 0], XYg[:, 1]).astype(np.float32)
            C = img_cam[Ig]
        ag, beta, wg = _fit_gains(Ig, Tg, Lg, len(names), P, C, len(cam_names))
        used = np.unique(Ig)
        a[used] = ag[used]
        n_obs += np.bincount(Ig, wg, len(names))
        if vignetting:
            n_cam_obs = np.bincount(C[wg], minlength=len(cam_names))
            for k, cam in enumerate(cam_names):
                if 0 < n_cam_obs[k] < 100 * VIGN_TERMS:
                    log(f'  vignetting ({label}, {_text(cam)}): not estimated, too few observations ({n_cam_obs[k]})')
                models.append((str(label), _text(cam), beta[k].tolist()))
            for i in used:
                vign[_stem(names[i])] = beta[img_cam[i]].tolist()
    gains = np.clip(np.exp(-a), 0.5, 2.0)
    return ({_stem(n): tuple(float(x) for x in gains[k]) for k, n in enumerate(names)},
            {_stem(n): int(n_obs[k]) for k, n in enumerate(names)},
            vign, models)


def _project_distorted(cam, x, y):
    """
    Normalized image coordinates of the bearing (x, y, 1) in an OpenSfM camera (same formulas as OpenSfM
    geometry/camera_distortions_functions.h): 'brown' (k1, k2, k3, p1, p2, focal_x, focal_y, c_x, c_y) and
    'perspective' (k1, k2, focal). Returns None for other camera models.
    """
    pt = cam.get('projection_type', 'perspective')
    k1, k2 = cam.get('k1', 0.0), cam.get('k2', 0.0)
    r2 = x * x + y * y
    if pt == 'brown':
        k3, p1, p2 = cam.get('k3', 0.0), cam.get('p1', 0.0), cam.get('p2', 0.0)
        radial = 1 + r2 * (k1 + r2 * (k2 + r2 * k3))
        xd = x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
        yd = y * radial + 2 * p2 * x * y + p1 * (r2 + 2 * y * y)
        return cam['focal_x'] * xd + cam.get('c_x', 0.0), cam['focal_y'] * yd + cam.get('c_y', 0.0)
    if pt == 'perspective':
        radial = 1 + r2 * (k1 + r2 * k2)
        return cam['focal'] * x * radial, cam['focal'] * y * radial
    return None


def vignetting_factor(beta, width, height, rows, cam=None, ucam=None):
    """
    Correction factor exp(-V(x, y)) (rows x width x 3) of the vignetting for some rows of an UNDISTORTED image:
    the pixel is mapped back to its position in the original (distorted) image, where the vignetting was fitted,
    through the undistorted camera ucam (OpenSfM 'perspective') and the original camera cam. Without the cameras
    (or with a camera model that is not handled), the position in the undistorted image is used (approximation).
    """
    import numpy as np
    size = max(width, height)
    xn = ((np.arange(width) + 0.5 - width / 2.0) / size)[None, :].repeat(len(rows), 0)
    yn = ((np.asarray(rows) + 0.5 - height / 2.0) / size)[:, None].repeat(width, 1)
    u, v = xn, yn
    if cam is not None and ucam is not None and ucam.get('projection_type', 'perspective') == 'perspective':
        f, k1, k2 = ucam['focal'], ucam.get('k1', 0.0), ucam.get('k2', 0.0)
        x, y = xn / f, yn / f
        for _ in range(5 if (k1 or k2) else 0):         # inverse of the radial distortion (0 for OpenSfM output)
            r2 = x * x + y * y
            d = 1 + r2 * (k1 + r2 * k2)
            x, y = xn / f / d, yn / f / d
        uv = _project_distorted(cam, x, y)
        if uv is not None:
            u, v = uv
    terms = _vign_terms((2 * u).astype(np.float32), (2 * v).astype(np.float32))
    return np.clip(np.exp(-(terms @ np.asarray(beta, np.float32))), 0.5, 2.0)


def save_color_gains(path, gains, n_obs, models=None, cameras=None):
    """color_gains.csv (one row per image) and, with the vignetting, vignetting.csv (one row per group, camera and
    channel: coefficients and correction factor at the image corners and at the middle of the long sides)."""
    with open(path, 'w', encoding='utf-8') as f:
        f.write('image,epoch,gain_r,gain_g,gain_b,n_obs\n')
        for k in sorted(gains):
            g = gains[k]
            f.write(f'{k},{epoch_of(k)},{g[0]:.4f},{g[1]:.4f},{g[2]:.4f},{n_obs.get(k, 0)}\n')
    if not models:
        return
    import numpy as np
    with open(os.path.join(os.path.dirname(path), 'vignetting.csv'), 'w', encoding='utf-8') as f:
        f.write('group,camera,channel,' + ','.join(f'c{j}' for j in range(VIGN_TERMS)) +
                ',factor_corner,factor_side\n')
        for group, cam, beta in models:
            c = (cameras or {}).get(cam, {})
            ratio = min(c.get('width', 3), c.get('height', 2)) / max(c.get('width', 3), c.get('height', 2))
            b = np.asarray(beta)
            corner = np.exp(-(_vign_terms(np.array([1., 1., -1., -1.]), np.array([ratio, -ratio, ratio, -ratio])) @ b)).mean(0)
            side = np.exp(-(_vign_terms(np.array([1., -1.]), np.array([0., 0.])) @ b)).mean(0)
            for ch, name in enumerate('rgb'):
                f.write(f'{group},"{cam}",{name},' + ','.join(f'{x:.6g}' for x in b[:, ch]) +
                        f',{corner[ch]:.4f},{side[ch]:.4f}\n')


def color_corrections(block_dir, gains, vign):
    """{image stem: correction} for apply_gain(): gain, vignetting and the cameras needed to map the vignetting."""
    if gains is None:
        return None
    cams, ucams, shot_cam = {}, {}, {}
    rec_path = os.path.join(block_dir, 'opensfm', 'reconstruction.json')
    urec_path = os.path.join(block_dir, 'opensfm', 'undistorted', 'reconstruction.json')
    for rec in _read_json(rec_path):
        cams.update(rec.get('cameras', {}))
        shot_cam.update({_stem(s): v.get('camera') for s, v in rec.get('shots', {}).items()})
    if os.path.isfile(urec_path):
        for rec in _read_json(urec_path):
            ucams.update(rec.get('cameras', {}))
    out = {}
    for stem, g in gains.items():
        cam = shot_cam.get(stem)
        out[stem] = {'gain': g, 'vign': (vign or {}).get(stem),
                     'cam': cams.get(cam), 'ucam': ucams.get(cam)}
    return out


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


def apply_gain(src, dst, corr):
    """
    Writes a colour-corrected copy of an (undistorted) image; the source, possibly hard-linked, is not modified.
    corr: (gain_r, gain_g, gain_b), or a dict from color_corrections() (gain and vignetting).
    """
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
    if not isinstance(corr, dict):
        corr = {'gain': corr, 'vign': None}
    gain = np.asarray(corr['gain'], np.float32)
    out = arr.astype(np.float32)
    if out.ndim == 3 and out.shape[-1] >= 3:
        h, w = out.shape[:2]
        if corr.get('vign') is not None:
            step = 256
            for r0 in range(0, h, step):
                rows = np.arange(r0, min(h, r0 + step))
                f = vignetting_factor(corr['vign'], w, h, rows, corr.get('cam'), corr.get('ucam'))
                out[r0:r0 + len(rows), :, :3] *= f * gain
        else:
            out[..., :3] *= gain
    vmax = np.iinfo(arr.dtype).max if np.issubdtype(arr.dtype, np.integer) else None
    if vmax is not None:
        out = np.clip(np.rint(out), 0, vmax)
    out = out.astype(arr.dtype)
    if os.path.exists(dst):
        os.remove(dst)
    write_tiff(dst, out)


def _split_options(options):
    """Splits the ODM options into the ones used by the SfM and the ones used after it (DENSE_OPTIONS)."""
    sfm, dense, k = [], [], 0
    while k < len(options):
        o = options[k]
        if o in EXEC_OPTIONS:
            k += 1 + EXEC_OPTIONS[o]
            continue
        n = DENSE_OPTIONS.get(o, (None,))[0]
        if n is None:
            sfm.append(o)
            k += 1
        else:
            dense += options[k:k + 1 + n]
            k += 1 + n
    return sfm, dense


def _options_dict(options):
    """{option: tuple of values} of a list of dense options (see DENSE_OPTIONS)."""
    out, k = {}, 0
    while k < len(options):
        n = DENSE_OPTIONS.get(options[k], (0,))[0]
        out[options[k]] = tuple(options[k + 1:k + 1 + n])
        k += 1 + n
    return out


def rerun_stage(old_ep, new_ep):
    """
    First ODM stage to run again for an epoch whose products were made with the parameters old_ep (None: nothing to
    run again; 'openmvs': the epoch project is rebuilt).
    """
    if old_ep == new_ep:
        return None
    if not old_ep or 'dense_options' not in old_ep:
        return 'openmvs'
    old, new = _options_dict(old_ep['dense_options']), _options_dict(new_ep['dense_options'])
    stages = [DENSE_OPTIONS.get(o, (0, 'openmvs'))[1] for o in set(old) | set(new) if old.get(o) != new.get(o)]
    if any(old_ep.get(k) != new_ep.get(k) for k in new_ep if k != 'dense_options'):
        stages.append('mvs_texturing')                  # colour calibration
    if not stages:
        return 'mvs_texturing'
    return min(stages, key=EPOCH_STAGES.index)


def _upgrade_params(old, workdir):
    """Parameters file of versions 0.0.0.9007-9010 (one list of options): converted, epochs marked as made with them."""
    if old.get('version') == 2:
        return old
    sfm, dense = _split_options(old.get('options', []))
    ep = {'dense_options': dense, 'calibrate_col': bool(old.get('calibrate_col')),
          'calibrate_col_by': 'chunk' if old.get('calibrate_col') else None}
    for epoch in old.get('images', {}):
        ortho = os.path.join(workdir, epoch, 'odm_orthophoto', 'odm_orthophoto.tif')
        ep_file = os.path.join(workdir, epoch, EPOCH_PARAMS_FILE)
        if os.path.isfile(ortho) and not os.path.isfile(ep_file):
            with open(ep_file, 'w', encoding='utf-8') as f:
                json.dump(ep, f, indent=1)
    return {'version': 2, 'images': old.get('images'), 'sfm_options': sfm}


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
                         calibrate_col_by='chunk',
                         calibrate_vignetting=True,
                         keypoint_limit=KEYPOINT_LIMIT,
                         from_mesh=False,
                         odm_gpu_fix=True,
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
    :param bool calibrate_col: colour calibration of the images on the tie points.
    :param str calibrate_col_by: 'chunk' (all the dates together: inside and between mosaics) or 'date' (each date
        alone: inside each mosaic only). See compute_color_gains().
    :param bool calibrate_vignetting: with calibrate_col, also corrects the vignetting (one polynomial per camera and
        channel, fitted on the same tie points), as Metashape calibrateColors() does.
    :param int keypoint_limit: number of keypoints per image (Metashape keypoint_limit; ODM --min-num-features,
        which is the number of features extracted per image).
    :param bool from_mesh: orthomosaic computed on the full 3D mesh (ODM --use-3dmesh) instead of the 2.5D one, as
        Metashape from_mesh; no DSM is then produced (as with Metashape).
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
    if odm_runner == 'native':
        check_odm_gpu(odm_path, fix=odm_gpu_fix)
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

    # Options chosen to reproduce the Metashape pipeline of canobsR (see the documentation of the engines):
    #  --ignore-gsd: products at resol_ref even when ODM estimates a coarser GSD, undistorted images at full size
    #                (by default ODM caps the resolution at its GSD estimate - 10 %);
    #  --crop 0: no buffer removed around the products (by default ODM removes 3 m along the edges);
    #  --gps-accuracy 10: accuracy of the GPS positions in the bundle adjustment (Metashape default: 10 m);
    #  --min-num-features: number of keypoints per image (Metashape keypoint_limit).
    options = ['--orthophoto-resolution', str(float(resol_ref) * 100),
               '--feature-quality', ALIGN_QUALITY[int(downscale_factor_alignement)],
               '--min-num-features', str(int(keypoint_limit)),
               '--gps-accuracy', str(GPS_ACCURACY),
               '--ignore-gsd',
               '--pc-quality', DEPTH_QUALITY[int(downscale_factor_depth_map)],
               '--crop', '0',
               '--orthophoto-compression', 'LZW',
               '--skip-report']
    if from_mesh:
        options += ['--use-3dmesh']
        if out_dir_DEM is not None:
            log('from_mesh: no DSM is produced (as with Metashape)')
            out_dir_DEM = None
    else:
        options += ['--skip-3dmodel']
    if out_dir_DEM is not None:
        options += ['--dsm', '--dem-resolution', str(float(resol_ref) * 100)]
    options += [str(a) for a in (odm_args or [])]

    if site_name:                                      # same file names as the Metashape pipeline: <site>_<date><suffix>
        site_name = site_name + '_'
    epochs = list_epochs(pathDIR, group_by_flight=group_by_flight, folder_names=folder_names, check_dates=check_dates)
    log(f'{len(epochs)} epochs: ' + ', '.join(f'{e} ({len(i)} images)' for e, i in epochs.items()))

    if calibrate_col_by not in ('chunk', 'date'):
        raise ValueError("calibrate_col_by must be 'chunk' or 'date'")

    # A previous run in workdir is resumed if it was made with the same images and the same SfM options (otherwise:
    # error). For each epoch, the products are kept if they were made with the same parameters; if only the colour
    # calibration changed, only the texture and the orthomosaic are computed again; otherwise the epoch is redone.
    sfm_options, dense_options = _split_options(options)
    params = {'version': 2, 'images': {e: sorted(n for _, n in i) for e, i in epochs.items()},
              'sfm_options': sfm_options}
    epoch_params = {'dense_options': dense_options, 'calibrate_col': bool(calibrate_col),
                    'calibrate_col_by': calibrate_col_by if calibrate_col else None,
                    'vignetting': bool(calibrate_vignetting) if calibrate_col else None}
    params_path = os.path.join(workdir, PARAMS_FILE)
    if [e for e in os.listdir(workdir) if e != TMP_NAME]:
        old = None
        if os.path.isfile(params_path):
            with open(params_path, encoding='utf-8') as f:
                old = _upgrade_params(json.load(f), workdir)
        if old is None or old['images'] != params['images'] or old['sfm_options'] != params['sfm_options']:
            raise TimeSiftError(f'{workdir} contains a previous run made with other images or SfM parameters '
                                f'(canobsR 0.0.0.9012 changed the SfM parameters to reproduce Metashape). '
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
        g, n_obs, vign, models = compute_color_gains(block_dir, by=calibrate_col_by, vignetting=calibrate_vignetting)
        cams = {}
        for rec in _read_json(os.path.join(block_dir, 'opensfm', 'reconstruction.json')):
            cams.update({_text(k): v for k, v in rec.get('cameras', {}).items()})
        save_color_gains(os.path.join(workdir, COLOR_FILE), g, n_obs, models, cams)
        gains = color_corrections(block_dir, g, vign)
        v = list(g.values())
        log(f'Colour calibration ({"all dates together" if calibrate_col_by == "chunk" else "date by date"}): '
            f'{len(v)} images, gains from {min(min(x) for x in v):.2f} to {max(max(x) for x in v):.2f} '
            f'(details in {COLOR_FILE})')
        if models:
            import numpy as np
            for group, cam, beta in models:
                ratio = 2 / 3
                c = cams.get(cam, {})
                if c.get('width') and c.get('height'):
                    ratio = min(c['width'], c['height']) / max(c['width'], c['height'])
                corner = float(np.exp(-(_vign_terms(np.array([1., 1., -1., -1.]),
                                                    np.array([ratio, -ratio, ratio, -ratio])) @ np.asarray(beta))).mean())
                log(f'  vignetting ({group}, {cam}): mean correction x{corner:.3f} at the image corners '
                    f'(details in vignetting.csv)')

    # 3. dense matching, DSM and orthomosaic for each epoch
    results = {}
    for epoch, images in epochs.items():
        te = time.time()
        valid = {_stem(n) for _, n in images}
        ortho = os.path.join(workdir, epoch, 'odm_orthophoto', 'odm_orthophoto.tif')
        ep_file = os.path.join(workdir, epoch, EPOCH_PARAMS_FILE)
        old_ep = None
        if os.path.isfile(ortho) and os.path.isfile(ep_file):
            with open(ep_file, encoding='utf-8') as f:
                old_ep = json.load(f)
        stage = rerun_stage(old_ep, epoch_params)
        if stage is None:
            log(f'Epoch {epoch}: orthomosaic already computed with the same parameters, skipped')
        else:
            if old_ep is not None and stage != 'openmvs':
                kept = {'odm_filterpoints': 'alignment and dense matching',
                        'odm_meshing': 'alignment and point cloud',
                        'mvs_texturing': 'alignment, point cloud and mesh'}[stage]
                log(f'Epoch {epoch}: parameters changed after the dense matching: ODM run again from {stage} '
                    f'({kept} kept)')
                retexture_epoch(workdir, epoch, images, gains)
                rerun = ['--rerun-from', stage]
            else:
                if os.path.isdir(os.path.join(workdir, epoch)):
                    shutil.rmtree(os.path.join(workdir, epoch))    # unfinished run, or other parameters
                make_epoch_project(workdir, epoch, images, gains)
                rerun = ['--rerun-from', 'openmvs']
            code = run_odm(odm_runner, odm_path, workdir, epoch, options + rerun, tmp_dir)
            if not os.path.isfile(ortho) and LAST_RUN['densify_failed'] and '--no-gpu' not in options:
                # OpenMVS failed, usually for lack of memory (GPU memory of a laptop, or RAM with many threads):
                # second try on the processor, with half of the threads. Same results, slower.
                n_thr = max(1, (os.cpu_count() or 2) // 2)
                log('*' * 100)
                log(f'Epoch {epoch}: the dense matching (OpenMVS) failed, probably for lack of memory. Second try on '
                    f'the processor (--no-gpu) with {n_thr} threads: same result, slower.')
                log('*' * 100)
                code = run_odm(odm_runner, odm_path, workdir, epoch,
                               options + ['--no-gpu', '--max-concurrency', str(n_thr), '--rerun-from', 'openmvs'], tmp_dir)
            if not os.path.isfile(ortho):
                hint = ''
                if LAST_RUN['densify_failed']:
                    hint = ('\nThe dense matching (OpenMVS) failed, also on the processor: probably not enough memory '
                            'for this computer. Options: downscale_factor_depth_map = 4 (depth maps at 1/4 of the image '
                            'size instead of 1/2: less memory, less detailed surface), or a computer with more memory. '
                            'The SfM is kept: running again resumes from the dense matching.')
                raise TimeSiftError(f'ODM failed for epoch {epoch} (exit code {code}). '
                                    f'See {os.path.join(workdir, "odm_" + epoch + ".log")}{hint}')
            with open(ep_file, 'w', encoding='utf-8') as f:
                json.dump(epoch_params, f, indent=1)
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
    p.add_argument('--calibrate_col_by', default='chunk', choices=['chunk', 'date'])
    p.add_argument('--no_vignetting', action='store_true')
    p.add_argument('--keypoint_limit', type=int, default=KEYPOINT_LIMIT)
    p.add_argument('--from_mesh', action='store_true')
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
                         calibrate_col=not args.no_calibrate_col, calibrate_col_by=args.calibrate_col_by,
                         calibrate_vignetting=not args.no_vignetting, keypoint_limit=args.keypoint_limit,
                         from_mesh=args.from_mesh,
                         check_dates=not args.no_check_dates,
                         odm_path=args.odm_path, odm_runner=args.odm_runner, odm_args=odm_args,
                         overwrite=args.overwrite, keep_project=True if args.keep_project else None)


# reticulate::source_python() also runs this file as '__main__': the command line is only parsed when it is used
if __name__ == '__main__' and any(a.startswith('--pathDIR') for a in sys.argv[1:]):
    main()
