from __future__ import annotations

import argparse
import json
import math
import shutil
import stat
import struct
import subprocess
import tempfile
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

VIDEO_SIZE = (1080, 1528)
VIDEO_FPS = 30
UPSCALE = 2
BACKGROUND_DPI = 144
ZOOM_MARGIN = 85
MIN_ZOOM_WIDTH = 820
MAX_ZOOM_WIDTH = 820
CAMERA_STROKE_WINDOW = 12
MAX_RENDERABLE_STROKE_WIDTH = 20.0
STROKE_WIDTH_SCALE = 1.35


@dataclass
class Stroke:
    identifier: str
    page: str
    color: tuple[int, int, int, int]
    width: float
    paths: list[tuple[float, float, float, float, float]]


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("Unexpected end of protobuf varint")


def protobuf_fields(data: bytes) -> list[tuple[int, int, Any]]:
    offset = 0
    result = []
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        field_number, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 1:
            value = data[offset:offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = read_varint(data, offset)
            value = data[offset:offset + length]
            offset += length
            if len(value) != length:
                raise ValueError("Unexpected end of length-delimited protobuf field")
        elif wire_type == 5:
            value = data[offset:offset + 4]
            offset += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")
        result.append((field_number, wire_type, value))
    return result


def length_delimited_records(data: bytes):
    offset = 0
    while offset < len(data):
        length, offset = read_varint(data, offset)
        end = offset + length
        if end > len(data):
            raise ValueError("Truncated length-delimited record")
        yield data[offset:end]
        offset = end


def field_value(fields, number: int, wire_type: int | None = None):
    for field_number, field_wire_type, value in fields:
        if field_number == number and (wire_type is None or field_wire_type == wire_type):
            return value
    return None


def decode_geometry(data: bytes) -> bytes:
    from lz4.block import decompress as lz4_decompress

    output = bytearray()
    offset = 0
    while data[offset:offset + 4] != b"bv4$":
        marker = data[offset:offset + 4]
        if marker == b"bv41":
            if offset + 12 > len(data):
                raise ValueError("Truncated Goodnotes LZ4 frame header")
            unpacked_length = int.from_bytes(data[offset + 4:offset + 8], "little")
            packed_length = int.from_bytes(data[offset + 8:offset + 12], "little")
            start = offset + 12
            end = start + packed_length
            if end > len(data):
                raise ValueError("Truncated Goodnotes LZ4 frame")
            output.extend(lz4_decompress(data[start:end], uncompressed_size=unpacked_length))
            offset = end
        elif marker == b"bv4-":
            if offset + 8 > len(data):
                raise ValueError("Truncated uncompressed Goodnotes frame header")
            length = int.from_bytes(data[offset + 4:offset + 8], "little")
            start = offset + 8
            end = start + length
            if end > len(data):
                raise ValueError("Truncated uncompressed Goodnotes frame")
            output.extend(data[start:end])
            offset = end
        else:
            raise ValueError(f"Unknown Goodnotes geometry frame at byte {offset}: {marker!r}")
    if offset + 4 != len(data):
        raise ValueError("Unexpected data after Goodnotes geometry terminator")
    return bytes(output)


def parse_signature(signature: str) -> list[Any]:
    def parse_one(offset: int):
        if signature.startswith("A(", offset) or signature.startswith("S(", offset):
            kind = signature[offset]
            inner_start = offset + 2
            depth = 1
            end = inner_start
            while end < len(signature) and depth:
                if signature[end] == "(":
                    depth += 1
                elif signature[end] == ")":
                    depth -= 1
                end += 1
            if depth:
                raise ValueError(f"Malformed Goodnotes geometry signature: {signature}")
            inner = signature[inner_start:end - 1]
            if kind == "A":
                element, consumed = parse_one(inner_start)
                if consumed != end - 1:
                    raise ValueError(f"Malformed array signature: {signature}")
                return ("array", element), end
            return ("struct", parse_signature(inner)), end
        if offset >= len(signature) or signature[offset] not in "uvf":
            raise ValueError(f"Unknown Goodnotes geometry token in {signature!r}")
        return ("scalar", signature[offset]), offset + 1

    result = []
    position = 0
    while position < len(signature):
        token, position = parse_one(position)
        result.append(token)
    return result


def decode_typed_value(data: bytes, offset: int, spec: Any):
    kind = spec[0]
    if kind == "scalar":
        if spec[1] == "v":
            if offset + 2 > len(data):
                raise ValueError("Truncated u16 in Goodnotes geometry")
            return int.from_bytes(data[offset:offset + 2], "little"), offset + 2
        if offset + 4 > len(data):
            raise ValueError("Truncated float in Goodnotes geometry")
        return struct.unpack_from("<f", data, offset)[0], offset + 4
    if kind == "struct":
        values = []
        for child in spec[1]:
            value, offset = decode_typed_value(data, offset, child)
            values.append(value)
        return tuple(values), offset
    if kind == "array":
        if offset + 4 > len(data):
            raise ValueError("Truncated array length in Goodnotes geometry")
        count = int.from_bytes(data[offset:offset + 4], "little")
        offset += 4
        values = []
        for _ in range(count):
            value, offset = decode_typed_value(data, offset, spec[1])
            values.append(value)
        return values, offset
    raise ValueError(f"Unsupported geometry value kind: {kind}")


def parse_geometry(data: bytes) -> tuple[str, list[Any]]:
    if data[:4] != b"tpl\0" or len(data) < 8:
        raise ValueError("Unrecognized Goodnotes stroke geometry")
    declared_size = int.from_bytes(data[4:8], "little")
    signature_end = data.find(b"\0", 8)
    if signature_end < 0:
        raise ValueError("Missing geometry signature terminator")
    signature = data[8:signature_end].decode("ascii")
    specs = parse_signature(signature)
    offset = signature_end + 1
    values = []
    for spec in specs:
        value, offset = decode_typed_value(data, offset, spec)
        values.append(value)
    if offset != len(data) or declared_size != len(data):
        raise ValueError("Goodnotes geometry did not parse to its declared length")
    return signature, values


def color_from_stroke(stroke_fields) -> tuple[int, int, int, int]:
    color_bytes = field_value(stroke_fields, 4, 2)
    rgba = [0.0, 0.0, 0.0, 1.0]
    if color_bytes is not None:
        for number, wire_type, value in protobuf_fields(color_bytes):
            if wire_type == 5 and 1 <= number <= 4:
                rgba[number - 1] = struct.unpack("<f", value)[0]
    return tuple(max(0, min(255, round(channel * 255))) for channel in rgba)


def page_content_id(entity_id: str) -> str:
    parts = entity_id.split("-")
    if len(parts) != 5 or len(parts[-1]) != 12:
        raise ValueError(f"Unexpected Goodnotes page entity UUID: {entity_id}")
    parts[-1] = f"{(int(parts[-1], 16) + 1) & 0xFFFFFFFFFFFF:012X}"
    return "-".join(parts)


def parse_stroke(stroke_bytes: bytes, page: str) -> Stroke:
    fields = protobuf_fields(stroke_bytes)
    identifier = field_value(fields, 1, 2).decode("ascii")
    geometry = decode_geometry(field_value(fields, 2, 2))
    signature, values = parse_geometry(geometry)
    paths: list[tuple[float, float, float, float, float]] = []
    width = 1.5

    if signature == "vuA(v)A(S(uu))A(S(uuuu))vA(f)":
        width = max(0.2, float(values[1]))
        for x1, y1, x2, y2 in values[4]:
            paths.append((x1, y1, x2, y2, width))
    elif signature == "vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)":
        flags = values[2][0] if values[2] else 0
        path = values[4]
        if flags & 4:
            if len(path) % 9:
                raise ValueError("Invalid paired-sample pressure stroke")
            for i in range(0, len(path), 9):
                x1, y1, w1, x2, y2, w2 = path[i:i + 6]
                paths.append((x1, y1, x2, y2, max(0.2, (w1 + w2) / 2)))
        else:
            if len(path) % 3:
                raise ValueError("Invalid triplet pressure stroke")
            points = [path[i:i + 3] for i in range(0, len(path), 3)]
            for first, second in zip(points, points[1:]):
                x1, y1, w1 = first
                x2, y2, w2 = second
                paths.append((x1, y1, x2, y2, max(0.2, (w1 + w2) / 2)))
    elif signature == (
        "vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)"
        "A(S(uu))A(S(uuuu))A(u)"
    ):
        # Newer Goodnotes versions emit empty placeholder records with this
        # signature; they contain no drawable point data.
        pass
    else:
        raise ValueError(f"Unsupported Goodnotes pen geometry: {signature}")

    if not paths:
        shape_bytes = field_value(fields, 9, 2)
        if shape_bytes:
            shape_fields = protobuf_fields(shape_bytes)
            points = []
            point_list = field_value(shape_fields, 1, 2)
            if point_list is not None:
                point_records = [
                    value
                    for number, wire, value in protobuf_fields(point_list)
                    if number == 1 and wire == 2
                ]
            else:
                shape_points = field_value(shape_fields, 3, 2)
                point_records = [
                    value
                    for _, wire, value in protobuf_fields(shape_points or b"")
                    if wire == 2
                ]
            for point_bytes in point_records:
                point_fields = protobuf_fields(point_bytes)
                x_bytes = field_value(point_fields, 1, 5)
                y_bytes = field_value(point_fields, 2, 5)
                if x_bytes is None or y_bytes is None:
                    raise ValueError(f"Malformed shape point in Goodnotes stroke {identifier}")
                points.append((struct.unpack("<f", x_bytes)[0], struct.unpack("<f", y_bytes)[0]))
            shape_width_bytes = field_value(shape_fields, 15, 5)
            if shape_width_bytes is not None:
                width = max(0.2, struct.unpack("<f", shape_width_bytes)[0])
            if len(points) < 2:
                raise ValueError(f"Unsupported shape geometry in Goodnotes stroke {identifier}")
            for first, second in zip(points, points[1:] + points[:1]):
                paths.append((*first, *second, width))
    return Stroke(identifier, page, color_from_stroke(fields), width, paths)


def read_document(root: Path):
    event_records = list(length_delimited_records((root / "index.events.pb").read_bytes()))
    sync_record = None
    page_width = None
    page_height = None
    for record in event_records:
        for number, wire, value in protobuf_fields(record):
            if number == 160 and wire == 2:
                if sync_record is not None:
                    raise ValueError("More than one Goodnotes audio synchronization record was found")
                sync_record = protobuf_fields(value)
            elif number == 2 and wire == 2:
                paper_fields = protobuf_fields(value)
                dimensions = field_value(paper_fields, 8, 2)
                if dimensions is not None:
                    dimension_fields = protobuf_fields(dimensions)
                    width_bytes = field_value(dimension_fields, 1, 5)
                    height_bytes = field_value(dimension_fields, 2, 5)
                    if width_bytes is None or height_bytes is None:
                        raise ValueError("The Goodnotes paper definition has no page dimensions")
                    page_width = struct.unpack("<f", width_bytes)[0]
                    page_height = struct.unpack("<f", height_bytes)[0]
                    if page_width <= 0 or page_height <= 0:
                        raise ValueError("The Goodnotes paper dimensions are invalid")
    if sync_record is None:
        raise ValueError("No Goodnotes audio synchronization record was found")
    if page_width is None or page_height is None:
        raise ValueError("No Goodnotes paper dimensions were found in the event journal")

    audio_id = field_value(sync_record, 2, 2).decode("ascii")
    declared_duration_ns = field_value(sync_record, 4, 0)
    sync_entries = []
    for number, wire, value in sync_record:
        if number != 5 or wire != 2:
            continue
        for _, entry_wire, entry_bytes in protobuf_fields(value):
            if entry_wire != 2:
                continue
            entry_fields = protobuf_fields(entry_bytes)
            stroke_id = field_value(entry_fields, 1, 2).decode("ascii")
            timing_fields = protobuf_fields(field_value(entry_fields, 2, 2))
            time_ns = field_value(timing_fields, 1, 0)
            page_entity = field_value(timing_fields, 2, 2).decode("ascii")
            sync_entries.append((time_ns / 1_000_000_000, stroke_id, page_content_id(page_entity)))
    if not sync_entries:
        raise ValueError("The Goodnotes recording event contains no stroke timestamps")

    pages: dict[str, dict[str, Stroke]] = {}
    for page_path in (root / "notes").iterdir():
        page_id = page_path.name.upper()
        strokes = {}
        for record in list(length_delimited_records(page_path.read_bytes()))[1:]:
            stroke_bytes = field_value(protobuf_fields(record), 7, 2)
            if stroke_bytes is None:
                continue
            stroke = parse_stroke(stroke_bytes, page_id)
            strokes[stroke.identifier] = stroke
        pages[page_id] = strokes

    audio_path = root / "attachments" / audio_id
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio attachment not found: {audio_path}")
    pdf_paths = list((root / "attachments").glob("*"))
    pdf_paths = [path for path in pdf_paths if path.read_bytes()[:5] == b"%PDF-"]
    if len(pdf_paths) != 1:
        raise ValueError(f"Expected one PDF paper attachment, found {len(pdf_paths)}")

    return (
        pages,
        sync_entries,
        audio_path,
        pdf_paths[0],
        declared_duration_ns / 1_000_000_000,
        page_width,
        page_height,
    )


def draw_stroke(canvas: Image.Image, stroke: Stroke, page_width: float):
    from PIL import ImageDraw

    scale = canvas.width / page_width
    draw = ImageDraw.Draw(canvas)
    red, green, blue, _ = stroke.color
    for x1, y1, x2, y2, width in stroke.paths:
        if width > MAX_RENDERABLE_STROKE_WIDTH:
            continue
        coords = (x1 * scale, y1 * scale, x2 * scale, y2 * scale)
        source_width = max(0.45, width) * STROKE_WIDTH_SCALE
        pixel_width = max(1, round(source_width * scale))
        draw.line(coords, fill=(red, green, blue, 255), width=pixel_width)
        radius = pixel_width / 2
        for x, y in ((coords[0], coords[1]), (coords[2], coords[3])):
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(red, green, blue, 255))


def page_crop_box(
    strokes: list[Stroke],
    page_width: float,
    page_height: float,
    canvas_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    return (0, 0, canvas_size[0], canvas_size[1])


def crop_with_background(image, box):
    from PIL import Image

    left, top, right, bottom = box
    width = max(2, right - left)
    height = max(2, bottom - top)
    cropped = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    source_left = max(0, left)
    source_top = max(0, top)
    source_right = min(image.width, right)
    source_bottom = min(image.height, bottom)
    if source_left < source_right and source_top < source_bottom:
        cropped.paste(
            image.crop((source_left, source_top, source_right, source_bottom)),
            (source_left - left, source_top - top),
        )
    return cropped


def fit_page_to_video(image):
    from PIL import Image

    return image.resize(VIDEO_SIZE, Image.Resampling.LANCZOS)


def probe_duration(ffprobe: str, audio_path: Path) -> float:
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(audio_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(json.loads(result.stdout)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The Goodnotes audio attachment has an invalid duration")
    return duration


def is_document_root(path: Path) -> bool:
    return (
        (path / "index.events.pb").is_file()
        and (path / "notes").is_dir()
        and (path / "attachments").is_dir()
    )


def locate_document_root(path: Path) -> Path:
    if is_document_root(path):
        return path
    matches = [
        candidate.parent
        for candidate in path.rglob("index.events.pb")
        if is_document_root(candidate.parent)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one Goodnotes document in {path}, found {len(matches)}"
        )
    return matches[0]


@contextmanager
def document_root(source: Path):
    source = source.expanduser().resolve()
    if source.is_dir():
        yield locate_document_root(source)
        return
    if not source.is_file() or not zipfile.is_zipfile(source):
        raise ValueError("Choose an extracted Goodnotes folder or a .goodnotes ZIP file")

    with tempfile.TemporaryDirectory(prefix="goodnotes-input-") as temporary:
        extraction_root = Path(temporary)
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                normalized = member.filename.replace("\\", "/")
                member_path = PurePosixPath(normalized)
                if (
                    member_path.is_absolute()
                    or any(part in ("", ".", "..") for part in member_path.parts)
                    or (member_path.parts and ":" in member_path.parts[0])
                ):
                    raise ValueError(f"Unsafe path in Goodnotes archive: {member.filename!r}")
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError(f"Symbolic links are not supported in archives: {member.filename!r}")

                destination = extraction_root.joinpath(*member_path.parts)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as input_file, destination.open("wb") as output_file:
                    shutil.copyfileobj(input_file, output_file)

        yield locate_document_root(extraction_root)


def render_video(root: Path, output: Path):
    from PIL import Image

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    pdftoppm = shutil.which("pdftoppm")
    if not ffmpeg or not ffprobe or not pdftoppm:
        raise RuntimeError("FFmpeg, ffprobe, and pdftoppm must be available on PATH")

    (
        pages,
        sync_entries,
        audio_path,
        paper_pdf,
        declared_duration,
        page_width,
        page_height,
    ) = read_document(root)
    if declared_duration <= 0:
        raise ValueError("The Goodnotes replay timing record has an invalid duration")
    audio_duration = probe_duration(ffprobe, audio_path)
    timing_scale = audio_duration / declared_duration
    mapped: dict[str, tuple[float, str]] = {}
    scaled_entries = [
        (timestamp * timing_scale, stroke_id, page_id)
        for timestamp, stroke_id, page_id in sync_entries
    ]
    for timestamp, stroke_id, page_id in scaled_entries:
        if stroke_id in mapped:
            raise ValueError(f"Duplicate audio synchronization entry for stroke {stroke_id}")
        if page_id not in pages or stroke_id not in pages[page_id]:
            raise ValueError(f"Audio synchronization entry references missing stroke {stroke_id}")
        mapped[stroke_id] = (timestamp, page_id)

    all_strokes = {stroke_id: stroke for page in pages.values() for stroke_id, stroke in page.items()}
    missing = set(mapped) - set(all_strokes)
    if missing:
        raise ValueError(f"{len(missing)} synchronized strokes are missing from the page data")

    synchronized_pages = defaultdict(list)
    for timestamp, stroke_id, page_id in scaled_entries:
        synchronized_pages[page_id].append(timestamp)
    page_order = sorted(
        synchronized_pages,
        key=lambda page_id: min(synchronized_pages[page_id]),
    )
    if not page_order:
        raise ValueError("No page ordering could be inferred from the audio synchronization data")

    unsynchronized: dict[str, list[Stroke]] = {
        page_id: [
            stroke for stroke_id, stroke in page_strokes.items()
            if stroke_id not in mapped
        ]
        for page_id, page_strokes in pages.items()
    }
    initial_page = page_order[0]
    final_page = page_order[-1]

    with tempfile.TemporaryDirectory(prefix="goodnotes-replay-") as temporary:
        work = Path(temporary)
        background_prefix = work / "paper"
        subprocess.run(
            [
                pdftoppm, "-f", "1", "-l", "1", "-r", str(BACKGROUND_DPI),
                "-png", "-singlefile", str(paper_pdf), str(background_prefix),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        background = Image.open(work / "paper.png").convert("RGBA")
        high_res_size = (
            round(page_width * UPSCALE),
            round(page_height * UPSCALE),
        )
        background = background.resize(high_res_size, Image.Resampling.LANCZOS)

        canvases = {
            page_id: background.copy()
            for page_id in pages
        }
        recent_strokes: dict[str, list[Stroke]] = defaultdict(list)
        for stroke in unsynchronized[initial_page]:
            draw_stroke(canvases[initial_page], stroke, page_width)
            recent_strokes[initial_page].append(stroke)
        recent_strokes[initial_page] = recent_strokes[initial_page][-CAMERA_STROKE_WINDOW:]

        timed_strokes = defaultdict(list)
        for timestamp, stroke_id, page_id in scaled_entries:
            if timestamp <= audio_duration:
                timed_strokes[timestamp].append(all_strokes[stroke_id])

        if unsynchronized[final_page]:
            tail_time = min(declared_duration * timing_scale, audio_duration)
            timed_strokes[tail_time].extend(unsynchronized[final_page])

        frame_number = 0
        frame_names: list[str] = []

        def save_frame() -> str:
            nonlocal frame_number
            name = f"frame_{frame_number:05d}.png"
            frame_number += 1
            crop_box = page_crop_box(
                recent_strokes[active_page],
                page_width,
                page_height,
                high_res_size,
            )
            image = fit_page_to_video(
                crop_with_background(canvases[active_page], crop_box)
            )
            image.convert("RGB").save(work / name, "PNG", compress_level=1)
            frame_names.append(name)
            return name

        active_page = initial_page
        save_frame()
        last_time = 0.0
        segments: list[tuple[str, float]] = []

        for timestamp in sorted(timed_strokes):
            timestamp = max(last_time, timestamp)
            if timestamp >= audio_duration:
                continue
            if timestamp > last_time:
                segments.append((frame_names[-1], timestamp - last_time))
                last_time = timestamp
            for stroke in timed_strokes[timestamp]:
                active_page = stroke.page
                draw_stroke(canvases[stroke.page], stroke, page_width)
                recent_strokes[stroke.page].append(stroke)
                recent_strokes[stroke.page] = recent_strokes[stroke.page][-CAMERA_STROKE_WINDOW:]
            save_frame()

        if last_time < audio_duration:
            segments.append((frame_names[-1], audio_duration - last_time))
        if not segments:
            raise ValueError("No video frames were generated")

        manifest = work / "replay.ffconcat"
        with manifest.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write("ffconcat version 1.0\n")
            for name, duration in segments:
                stream.write(f"file '{name}'\n")
                stream.write(f"duration {duration:.9f}\n")
            stream.write(f"file '{frame_names[-1]}'\n")

        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", manifest.name,
                "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-vf", f"fps={VIDEO_FPS}", "-pix_fmt", "yuv420p",
                "-r", str(VIDEO_FPS), "-fps_mode", "cfr",
                "-video_track_timescale", "90000", "-c:a", "copy",
                "-t", f"{audio_duration:.6f}", "-movflags", "+faststart",
                "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
                str(output.resolve()),
            ],
            check=True,
            cwd=work,
        )

    print(f"Created: {output.resolve()}")
    print(f"Audio duration: {audio_duration:.3f} seconds")
    print(f"Mapped ink strokes: {len(mapped)} / {len(all_strokes)}")
    print(f"Unsynchronized strokes: {len(all_strokes) - len(mapped)}")
    empty_items = sum(not stroke.paths for stroke in all_strokes.values())
    if empty_items:
        print(f"Empty items omitted: {empty_items}")
    print(f"Replay timing record duration: {declared_duration:.3f} seconds")
    print(f"Replay-to-audio timing scale: {timing_scale:.6f}x")


def render_source(source: Path, output: Path):
    from dependency_setup import ensure_dependencies

    ensure_dependencies()
    with document_root(source) as root:
        render_video(root, output)


def main():
    parser = argparse.ArgumentParser(
        description="Render synchronized Goodnotes handwriting and embedded audio as an MP4."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Goodnotes .goodnotes ZIP file or extracted document folder",
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("Goodnotes-Replay.mp4"),
        help="Output MP4 path (default: Goodnotes-Replay.mp4)",
    )
    args = parser.parse_args()
    render_source(args.input, args.output)


if __name__ == "__main__":
    main()
