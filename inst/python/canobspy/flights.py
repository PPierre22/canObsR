"""
Flight folders of a canobs site and their epochs.

A site folder (flights_dir) contains one sub-folder per flight, named <prefix>_<YYYYMMDD>[_<drone>], e.g.
    RGB_20250101               flight of the main drone of the site on 1 January 2025
    RGB_20250115_Matrice4E     flight of another drone (Matrice 4E) on 15 January 2025
The date may also be YYYYMM (monthly series). The epoch, which names the mosaic, is the date, followed by the drone
when there is one: "20250101", "20250115_Matrice4E". Two drones flown on the same day therefore give two mosaics.
"""
import os
import re

DATE_PATTERN = r'\d{4}(0[1-9]|1[0-2])([0-2][0-9]|3[01])?'
IMG_EXT = ('.jpg', '.jpeg', '.tif', '.tiff', '.png', '.dng')
RESERVED = {'timesift_block', 'time-sift-block'}


def _safe(name):
    return re.sub(r'[^A-Za-z0-9_-]', '_', name)


def parse_folder(name):
    """dict(folder, prefix, date, drone, epoch) of a flight folder name; date and epoch are None without a date."""
    m = re.search(DATE_PATTERN, name)
    if not m:
        return {'folder': name, 'prefix': name, 'date': None, 'drone': '', 'epoch': None}
    drone = _safe(name[m.end():].strip('_- .'))
    date = m.group()
    return {'folder': name, 'prefix': name[:m.start()].strip('_- .'), 'date': date, 'drone': drone,
            'epoch': f'{date}_{drone}' if drone else date}


def epoch_of_folder(name):
    """Epoch of a flight folder ('RGB_20250115_Matrice4E' -> '20250115_Matrice4E'), None without a date."""
    return parse_folder(name)['epoch']


def list_flights(flights_dir, count_images=True):
    """
    Flight folders of flights_dir: list of dicts (folder, prefix, date, drone, epoch, n_images). Folders without a
    date, hidden ones ('.', '_') and reserved names are returned with epoch None (ignored by the processing).
    """
    out = []
    for d in sorted(os.listdir(flights_dir)):
        p = os.path.join(flights_dir, d)
        if not os.path.isdir(p):
            continue
        rec = parse_folder(d)
        if d in RESERVED or d.startswith(('.', '_')):
            rec['epoch'] = None
        n = 0
        if count_images and rec['epoch']:
            for _, dirs, files in os.walk(p):
                dirs[:] = [x for x in dirs if x not in RESERVED]
                n += sum(f.lower().endswith(IMG_EXT) for f in files)
        rec['n_images'] = n
        out.append(rec)
    return out
