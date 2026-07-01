#!/usr/bin/env python3
"""
Build a Claude/Codex-ready digest from Instagram/Facebook saved-item exports.

What it does:
  1. Finds URLs buried in Meta JSON export files.
  2. Writes a de-duplicated urls.txt.
  3. Runs yt-dlp to fetch metadata and English auto-subtitles without video.
  4. Collapses .info.json + .srt files into digest.txt.
  5. Optionally splits digest.txt into chunk_0001.txt, chunk_0002.txt, ...

Requirements:
  - Python 3.9+
  - yt-dlp installed and on PATH:
      python -m pip install -U yt-dlp

Usage:
  python meta_saved_reels_digest.py "path/to/meta/export"

Optional:
  python meta_saved_reels_digest.py "path/to/export" --out reels_digest
  python meta_saved_reels_digest.py "path/to/export" --cookies cookies.txt
  python meta_saved_reels_digest.py "path/to/export" --cookies-from-browser chrome
  python meta_saved_reels_digest.py "path/to/export" --chunk-lines 2000
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from html import unescape
from pathlib import Path
from typing import Any, Iterable


URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
SRT_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}")
TAG_RE = re.compile(r"<[^>]+>")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Meta saved-item URLs and build a Claude/Codex digest."
    )
    parser.add_argument(
        "export_dir",
        type=Path,
        help="Folder containing Instagram/Facebook JSON export files.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("reels_digest_out"),
        help="Output folder. Default: reels_digest_out",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        help="Optional Netscape cookies.txt for Instagram/Facebook auth.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        choices=["brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi"],
        help="Optional browser cookie source passed to yt-dlp.",
    )
    parser.add_argument(
        "--chunk-lines",
        type=int,
        default=0,
        help="If set, split digest.txt into chunks with this many lines.",
    )
    parser.add_argument(
        "--skip-download-step",
        action="store_true",
        help="Only rebuild digest.txt from existing .info.json/.srt files.",
    )
    return parser.parse_args()


def iter_json_files(root: Path) -> Iterable[Path]:
    ignored_parts = {"node_modules", ".git", "__MACOSX"}
    for path in root.rglob("*.json"):
        if not any(part in ignored_parts for part in path.parts):
            yield path


def looks_like_meta_content_url(url: str) -> bool:
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    domains = (
        "instagram.com/",
        "www.instagram.com/",
        "facebook.com/",
        "www.facebook.com/",
        "fb.watch/",
        "m.facebook.com/",
    )
    if not any(domain in lowered for domain in domains):
        return False
    reject_fragments = (
        "/privacy/",
        "/help/",
        "/policies/",
        "/legal/",
        "/terms",
        "static.xx.fbcdn.net",
    )
    return not any(fragment in lowered for fragment in reject_fragments)


def normalize_url(url: str) -> str:
    url = unescape(url).strip().rstrip(".,;)")
    return url.replace("\\/", "/")


def extract_urls_from_value(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_l = str(key).lower()
            if key_l in {"href", "url", "uri"} and isinstance(nested, str):
                candidate = normalize_url(nested)
                if looks_like_meta_content_url(candidate):
                    yield candidate
            yield from extract_urls_from_value(nested)
    elif isinstance(value, list):
        for item in value:
            yield from extract_urls_from_value(item)
    elif isinstance(value, str):
        for match in URL_RE.findall(value):
            candidate = normalize_url(match)
            if looks_like_meta_content_url(candidate):
                yield candidate


def collect_urls(export_dir: Path) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for json_file in iter_json_files(export_dir):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            data = json.loads(json_file.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            print(f"warning: skipped unreadable JSON {json_file}: {exc}", file=sys.stderr)
            continue

        for url in extract_urls_from_value(data):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def run_yt_dlp(urls_file: Path, media_dir: Path, args: argparse.Namespace) -> None:
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-info-json",
        "--write-auto-sub",
        "--sub-lang",
        "en",
        "--convert-subs",
        "srt",
        "--sleep-requests",
        "3",
        "--min-sleep-interval",
        "4",
        "--max-sleep-interval",
        "9",
        "--ignore-errors",
        "--no-overwrites",
        "-a",
        str(urls_file),
    ]
    if args.cookies:
        cmd.extend(["--cookies", str(args.cookies)])
    if args.cookies_from_browser:
        cmd.extend(["--cookies-from-browser", args.cookies_from_browser])

    print("running yt-dlp; private/deleted/age-gated items may fail and be skipped")
    subprocess.run(cmd, cwd=media_dir, check=True)


def read_info(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return json.loads(path.read_text(encoding="utf-8-sig"))


def clean_srt(path: Path) -> str:
    lines: list[str] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = TAG_RE.sub("", raw_line).strip()
        if not line:
            continue
        if line.isdigit():
            continue
        if SRT_TIME_RE.match(line):
            continue
        lines.append(line)
    return " ".join(lines)


def find_subtitle_for(info_file: Path) -> Path | None:
    stem = info_file.name.removesuffix(".info.json")
    candidates = [
        info_file.with_name(f"{stem}.en.srt"),
        info_file.with_name(f"{stem}.en-US.srt"),
        info_file.with_name(f"{stem}.en-orig.srt"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    matches = sorted(info_file.parent.glob(f"{stem}*.srt"))
    return matches[0] if matches else None


def first_text(info: dict[str, Any], keys: Iterable[str], default: str = "none") -> str:
    for key in keys:
        value = info.get(key)
        if value:
            return str(value).strip()
    return default


def build_digest(media_dir: Path, digest_file: Path) -> int:
    info_files = sorted(media_dir.glob("*.info.json"))
    with digest_file.open("w", encoding="utf-8", newline="\n") as out:
        for index, info_file in enumerate(info_files, start=1):
            info = read_info(info_file)
            reel_id = info.get("id") or info_file.name.removesuffix(".info.json")
            url = first_text(info, ["webpage_url", "original_url", "url"])
            creator = first_text(info, ["uploader", "channel", "creator", "uploader_id"])
            title = first_text(info, ["title"], default="none")
            caption = first_text(info, ["description", "fulltitle"], default="none")

            out.write(f"=== {index}: {reel_id} ===\n")
            out.write(f"URL: {url}\n")
            out.write(f"Creator: {creator}\n")
            out.write(f"Title: {title}\n")
            out.write(f"Caption: {caption}\n")

            subtitle = find_subtitle_for(info_file)
            if subtitle:
                transcript = clean_srt(subtitle)
                out.write(f"Transcript: {transcript if transcript else 'none'}\n")
            else:
                out.write("Transcript: none\n")
            out.write("\n")
    return len(info_files)


def split_digest(digest_file: Path, chunk_lines: int) -> int:
    if chunk_lines <= 0:
        return 0

    for old_chunk in digest_file.parent.glob("chunk_*.txt"):
        old_chunk.unlink()

    chunk_index = 1
    line_count = 0
    chunk_file = digest_file.parent / f"chunk_{chunk_index:04d}.txt"
    out = chunk_file.open("w", encoding="utf-8", newline="\n")
    try:
        for line in digest_file.read_text(encoding="utf-8").splitlines(keepends=True):
            if line_count >= chunk_lines and line.startswith("=== "):
                out.close()
                chunk_index += 1
                line_count = 0
                chunk_file = digest_file.parent / f"chunk_{chunk_index:04d}.txt"
                out = chunk_file.open("w", encoding="utf-8", newline="\n")
            out.write(line)
            line_count += 1
    finally:
        out.close()

    return chunk_index


def main() -> int:
    args = parse_args()
    export_dir = args.export_dir.expanduser().resolve()
    out_dir = args.out.expanduser().resolve()
    media_dir = out_dir / "reels"
    urls_file = out_dir / "urls.txt"
    digest_file = out_dir / "digest.txt"

    if not export_dir.exists():
        print(f"error: export folder does not exist: {export_dir}", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_download_step:
        urls = collect_urls(export_dir)
        urls_file.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
        print(f"found {len(urls)} Meta content URLs -> {urls_file}")
        if not urls:
            print("error: no Instagram/Facebook content URLs found in JSON files", file=sys.stderr)
            return 1
        run_yt_dlp(urls_file, media_dir, args)
    else:
        print("skipping URL extraction and yt-dlp; rebuilding digest from existing files")

    count = build_digest(media_dir, digest_file)
    print(f"wrote {count} entries -> {digest_file}")

    if args.chunk_lines:
        chunks = split_digest(digest_file, args.chunk_lines)
        print(f"wrote {chunks} chunk file(s) -> {out_dir / 'chunk_0001.txt'}")

    print("\nPrompt for each digest/chunk:")
    print(
        'Sort each entry into AI or True Crime. For AI: creator, the specific '
        'tool/prompt/tactic, and one action item. For True Crime: documentary '
        'name and streaming service if stated, otherwise mark "not named." '
        "Output a table."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
