import argparse
import hashlib
import re
import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_range(url, part, first, last, total):
    length = last - first + 1
    for attempt in range(1, 5):
        written = part.stat().st_size if part.exists() else 0
        if written == length:
            return length
        if written > length:
            raise OSError(f"Unexpected size of range file: {part}")
        start = first + written
        request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{last}"})
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{last}/{total}":
                    raise OSError("Server did not return the requested byte range")
                with part.open("ab") as stream:
                    while chunk := response.read(1024 * 1024):
                        if written + len(chunk) > length:
                            raise OSError("Response exceeded the requested range")
                        stream.write(chunk)
                        written += len(chunk)
            if written != length:
                raise OSError("Incomplete byte range")
            return length
        except (OSError, EOFError):
            if attempt == 4:
                raise
            time.sleep(attempt * 2)


def fill_ranges(url, partial, size, offset, jobs):
    block_size = 32 * 1024 * 1024
    parts = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for first in range(offset, size, block_size):
            last = min(first + block_size, size) - 1
            part = partial.with_name(f"{partial.name}.{first}-{last}")
            parts[pool.submit(download_range, url, part, first, last, size)] = part
        for count, future in enumerate(as_completed(parts), start=1):
            offset += future.result()
            if count % 4 == 0 or count == len(parts):
                print(f"{partial.name}: {offset / size:.1%}", flush=True)
    with partial.open("ab") as output:
        for part in parts.values():
            with part.open("rb") as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
    return parts.values()


def download(url, destination, size, sha256, jobs=1):
    if destination.exists():
        if destination.stat().st_size != size or digest(destination) != sha256:
            raise SystemExit(f"Existing file failed verification: {destination}")
        print(f"Already verified: {destination.name}", flush=True)
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    offset = partial.stat().st_size if partial.exists() else 0
    parts = ()
    if jobs > 1 and offset < size:
        print(f"{destination.name}: starting {jobs} range workers at {offset:,}/{size:,}", flush=True)
        parts = fill_ranges(url, partial, size, offset, jobs)
    for attempt in range(1, 5):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset == size:
            break
        if offset > size:
            raise SystemExit(f"Partial file exceeds expected size: {partial}")
        request = urllib.request.Request(
            url, headers={"Range": f"bytes={offset}-"} if offset else {}
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                if response.status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if not match or int(match[1]) != offset or int(match[3]) != size:
                        raise OSError(f"Unexpected Content-Range: {content_range}")
                elif response.status == 200:
                    if response.length is not None and response.length != size:
                        raise OSError(f"Unexpected Content-Length: {response.length}")
                    offset = 0
                else:
                    raise OSError(f"Unexpected HTTP status: {response.status}")

                print(f"{destination.name}: starting at {offset:,}/{size:,}", flush=True)
                report_at = time.monotonic() + 10
                with partial.open("ab" if offset else "wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        if offset + len(chunk) > size:
                            raise OSError("Response exceeded the expected file size")
                        stream.write(chunk)
                        offset += len(chunk)
                        if time.monotonic() >= report_at:
                            print(f"{destination.name}: {offset / size:.1%}", flush=True)
                            report_at = time.monotonic() + 10
            if offset == size:
                break
            raise OSError(f"Download stopped at {offset:,}/{size:,}")
        except (OSError, EOFError) as exc:
            if attempt == 4:
                raise
            print(f"{destination.name}: retry {attempt}: {exc}", flush=True)
            time.sleep(attempt * 2)

    if partial.stat().st_size != size or digest(partial) != sha256:
        raise SystemExit(f"Downloaded file failed SHA-256 verification: {partial}")
    partial.replace(destination)
    for part in parts:
        part.unlink()
    print(f"Verified {destination.name} ({size:,} bytes, SHA-256 {sha256})", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("destination", type=Path)
    parser.add_argument("size", type=int)
    parser.add_argument("sha256")
    parser.add_argument("--jobs", type=int, choices=range(1, 17), default=1)
    args = parser.parse_args()
    download(args.url, args.destination, args.size, args.sha256, args.jobs)
