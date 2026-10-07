from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable


Progress = Callable[[str], None]
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "GoodnotesReplay-DependencySetup",
}
PYTHON_PACKAGES = (("PIL", "Pillow", (10, 0)), ("lz4", "lz4", (4, 3)))


def support_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "GoodnotesReplay"
    return Path.home() / ".goodnotes-replay"


def _request(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers=headers or {"User-Agent": GITHUB_HEADERS["User-Agent"]})
    return urllib.request.urlopen(request, timeout=60)


def _github_json(url: str) -> dict:
    with _request(url, GITHUB_HEADERS) as response:
        return json.load(response)


def _package_available(
    module: str,
    distribution: str,
    minimum_version: tuple[int, ...],
    target: Path | None = None,
) -> bool:
    if target:
        sys.path.insert(0, str(target))
    try:
        if importlib.util.find_spec(module) is None:
            return False
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            return False
        version = tuple(int(part) for part in re.findall(r"\d+", installed)[:3])
        return version >= minimum_version
    finally:
        if target:
            sys.path.remove(str(target))


def ensure_python_packages(progress: Progress):
    target = support_directory() / "python-packages"
    target.mkdir(parents=True, exist_ok=True)
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))

    missing = [
        (module, distribution, minimum_version)
        for module, distribution, minimum_version in PYTHON_PACKAGES
        if not _package_available(module, distribution, minimum_version)
        and not _package_available(module, distribution, minimum_version, target)
    ]
    if not missing:
        return

    progress("Installing missing Python packages in your user profile...")
    requirements = Path(__file__).resolve().with_name("requirements.txt")
    if not requirements.is_file():
        raise FileNotFoundError(f"Python package list not found: {requirements}")

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--target",
        str(target),
        "-r",
        str(requirements),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode and "No module named pip" in (result.stderr + result.stdout):
        bootstrap = subprocess.run(
            [sys.executable, "-m", "ensurepip", "--user"],
            capture_output=True,
            text=True,
        )
        if bootstrap.returncode == 0:
            result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            "Could not install the Python packages. Check your internet connection "
            "and that this Python installation includes pip.\n\n"
            f"{detail}"
        )

    unavailable = [
        distribution for module, distribution, version in missing
        if not _package_available(module, distribution, version, target)
    ]
    if unavailable:
        raise RuntimeError(
            "Python package installation completed, but these packages cannot be imported: "
            + ", ".join(unavailable)
        )


def _download_verified(
    url: str,
    destination: Path,
    expected_sha256: str,
    progress: Progress,
    label: str,
):
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": GITHUB_HEADERS["User-Agent"]})
    temporary = destination.with_suffix(destination.suffix + ".download")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            total = int(response.headers.get("Content-Length", "0"))
            received = 0
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if total:
                    progress(f"Downloading {label}: {received / total:.0%}")
        actual_sha256 = digest.hexdigest()
        if actual_sha256.lower() != expected_sha256.lower():
            raise RuntimeError(
                f"The downloaded {label} failed its SHA-256 check. "
                "The file was rejected."
            )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_zip_entries(archive: zipfile.ZipFile):
    entries = []
    for info in archive.infolist():
        normalized = info.filename.replace("\\", "/")
        member = PurePosixPath(normalized)
        if (
            member.is_absolute()
            or any(part in ("", ".", "..") for part in member.parts)
            or (member.parts and ":" in member.parts[0])
        ):
            raise ValueError(f"Unsafe path in dependency archive: {info.filename!r}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError(f"Symbolic link in dependency archive: {info.filename!r}")
        entries.append((info, member))
    return entries


def _extract_tool_bin(
    archive_path: Path,
    executable_name: str,
    destination: Path,
    progress: Progress,
):
    staging = destination.with_name(destination.name + ".installing")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = _safe_zip_entries(archive)
            executable_dirs = [
                member.parent
                for info, member in entries
                if not info.is_dir() and member.name.lower() == executable_name.lower()
            ]
            if len(executable_dirs) != 1:
                raise ValueError(
                    f"Expected one {executable_name} in the downloaded tool archive, "
                    f"found {len(executable_dirs)}"
                )
            tool_bin = executable_dirs[0]
            files = [
                info for info, member in entries
                if not info.is_dir() and member.parent == tool_bin
            ]
            if not any(
                PurePosixPath(info.filename.replace("\\", "/")).name.lower()
                == executable_name.lower()
                for info in files
            ):
                raise ValueError(f"{executable_name} was not present in its tool folder")
            progress(f"Installing {executable_name} for this user...")
            for info in files:
                filename = PurePosixPath(info.filename.replace("\\", "/")).name
                target = staging / filename
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _latest_github_asset(repository: str, name: str) -> tuple[str, str]:
    release = _github_json(f"https://api.github.com/repos/{repository}/releases/latest")
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == name]
    if len(assets) != 1:
        raise RuntimeError(f"Could not find {name} in the latest {repository} release")
    asset = assets[0]
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:"):
        raise RuntimeError(f"The {name} release does not publish a SHA-256 checksum")
    return asset["browser_download_url"], digest.split(":", 1)[1]


def _install_ffmpeg(progress: Progress):
    base = support_directory() / "tools" / "ffmpeg"
    executable = base / "ffmpeg.exe"
    probe = base / "ffprobe.exe"
    if executable.is_file() and probe.is_file():
        return

    progress("Looking up the latest Windows FFmpeg build...")
    url, digest = _latest_github_asset(
        "BtbN/FFmpeg-Builds",
        "ffmpeg-master-latest-win64-gpl-shared.zip",
    )
    with tempfile.TemporaryDirectory(prefix="goodnotes-ffmpeg-") as temporary:
        package = Path(temporary) / "ffmpeg.zip"
        _download_verified(url, package, digest, progress, "FFmpeg")
        _extract_tool_bin(package, "ffmpeg.exe", base, progress)
    if not executable.is_file() or not probe.is_file():
        raise RuntimeError("The FFmpeg archive did not contain both ffmpeg.exe and ffprobe.exe")


def _install_poppler(progress: Progress):
    base = support_directory() / "tools" / "poppler"
    executable = base / "pdftoppm.exe"
    if executable.is_file():
        return

    progress("Looking up the latest Windows Poppler build...")
    release = _github_json(
        "https://api.github.com/repos/oschwartz10612/poppler-windows/releases/latest"
    )
    assets = [
        asset for asset in release.get("assets", [])
        if asset.get("name", "").lower().endswith(".zip")
        and asset.get("name", "").startswith("Release-")
    ]
    if len(assets) != 1:
        raise RuntimeError("Could not find the Windows Poppler ZIP in its latest release")
    asset = assets[0]
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:"):
        raise RuntimeError("The Poppler release does not publish a SHA-256 checksum")

    with tempfile.TemporaryDirectory(prefix="goodnotes-poppler-") as temporary:
        package = Path(temporary) / "poppler.zip"
        _download_verified(
            asset["browser_download_url"],
            package,
            digest.split(":", 1)[1],
            progress,
            "Poppler",
        )
        _extract_tool_bin(package, "pdftoppm.exe", base, progress)
    if not executable.is_file():
        raise RuntimeError("The Poppler archive did not contain pdftoppm.exe")


def _ffmpeg_supports_h264_encoder(executable: str) -> bool:
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    return result.returncode == 0 and "libx264" in result.stdout


def _poppler_is_usable(executable: str) -> bool:
    try:
        result = subprocess.run(
            [executable, "-v"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return False
    return result.returncode == 0


def ensure_dependencies(progress: Progress = lambda _: None):
    if sys.platform != "win32":
        raise RuntimeError("Automatic dependency downloads are currently supported on Windows only")

    ensure_python_packages(progress)

    ffmpeg = shutil.which("ffmpeg")
    needs_ffmpeg = (
        ffmpeg is None
        or shutil.which("ffprobe") is None
        or not _ffmpeg_supports_h264_encoder(ffmpeg)
    )
    if needs_ffmpeg:
        progress("A compatible FFmpeg build was not found; checking the portable user install...")
        _install_ffmpeg(progress)
    pdftoppm = shutil.which("pdftoppm")
    needs_poppler = pdftoppm is None or not _poppler_is_usable(pdftoppm)
    if needs_poppler:
        progress("A working Poppler build was not found; checking the portable user install...")
        _install_poppler(progress)

    tool_root = support_directory() / "tools"
    tool_paths = []
    if needs_ffmpeg:
        tool_paths.append(tool_root / "ffmpeg")
    if needs_poppler:
        tool_paths.append(tool_root / "poppler")
    os.environ["PATH"] = os.pathsep.join(
        [*(str(path) for path in tool_paths if path.is_dir()), os.environ.get("PATH", "")]
    )
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    pdftoppm = shutil.which("pdftoppm")
    if (
        ffmpeg is None
        or ffprobe is None
        or not _ffmpeg_supports_h264_encoder(ffmpeg)
    ):
        raise RuntimeError("FFmpeg and its H.264 encoder could not be made available")
    if pdftoppm is None or not _poppler_is_usable(pdftoppm):
        raise RuntimeError("Poppler's pdftoppm could not be made available")
    return {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "pdftoppm": pdftoppm,
    }
