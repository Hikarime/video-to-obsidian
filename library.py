"""Portable Obsidian storage shared by both video providers."""

from dataclasses import dataclass
import json
from pathlib import Path
import re

import bilibili as bili


PLATFORMS = ("bilibili", "youtube")


def valid_id(platform, identifier):
    pattern = r"BV[A-Za-z0-9]{10}" if platform == "bilibili" else r"[A-Za-z0-9_-]{11}"
    return platform in PLATFORMS and isinstance(identifier, str) and re.fullmatch(pattern, identifier) is not None


@dataclass(frozen=True)
class Video:
    platform: str
    identifier: str
    title: str
    uploader: str
    url: str
    cover_url: str
    duration_seconds: int
    parts: tuple = ()


class Library:
    def __init__(self, root, platform):
        self.root = Path(root).resolve()
        self.platform = platform
        self.notes = self.root / "videos"
        self.covers = self.root / "covers"
        self.vault = next((path for path in (self.root, *self.root.parents)
                           if (path / ".obsidian").is_dir()), None)
        if self.vault is None:
            raise ValueError("Place the tool and collections inside an Obsidian vault.")

    def relative(self, path):
        return Path(path).relative_to(self.vault).as_posix()

    def note_path(self, identifier):
        if not valid_id(self.platform, identifier):
            raise bili.ImportProblem("Invalid video ID.")
        return self.notes / f"{identifier}.md"

    def has_video(self, identifier):
        return self.note_path(identifier).is_file()

    def count(self):
        return sum(valid_id(self.platform, path.stem) for path in self.notes.glob("*.md"))

    def title(self, identifier):
        try:
            with self.note_path(identifier).open(encoding="utf-8-sig") as stream:
                lines = stream.read(64 * 1024).splitlines()
            if lines and lines[0] == "---":
                for line in lines[1:]:
                    if line == "---":
                        break
                    if line.startswith("title:"):
                        value = line[6:].strip()
                        if value.startswith('"'):
                            value = json.loads(value)
                        elif value.startswith("'") and value.endswith("'"):
                            value = value[1:-1].replace("''", "'")
                        return str(value) or identifier
        except (OSError, UnicodeError, ValueError):
            pass
        return identifier

    def prepare(self):
        self.notes.mkdir(parents=True, exist_ok=True)
        self.covers.mkdir(parents=True, exist_ok=True)
        gallery = self.root / "Gallery.base"
        if not gallery.exists():
            template = Path(__file__).with_name("templates") / "Gallery.base"
            bili.atomic_write(gallery, template.read_bytes(), replace=False)

    def render_note(self, video, cover):
        properties = {
            "title": video.title, "uploader": video.uploader, "url": video.url,
            "cover": f"[[{self.relative(cover)}]]",
            "duration": bili.format_duration(video.duration_seconds),
            "watched_time": "00:00", "tags": [],
        }
        lines = ["---", *(f"{key}: {bili.yaml_scalar(value)}" for key, value in properties.items()), "---", ""]
        if video.platform == "bilibili" and len(video.parts) > 1:
            lines.extend([f"> [!info]- Parts ({len(video.parts)})", ">",
                          "> | Part | Title | Duration | Play |", "> | --- | --- | --- | --- |"])
            for part in video.parts:
                lines.append(f"> | P{part.number} | {bili.markdown_text(part.title)} | "
                             f"{bili.format_duration(part.duration)} | [Watch]({video.url}?p={part.number}) |")
            lines.append("")
        lines.extend(["## Notes", "", ""])
        return "\n".join(lines)

    def save(self, video, cover_data, extension):
        note = self.note_path(video.identifier)
        if video.platform != self.platform:
            raise bili.ImportProblem("Video platform does not match the collection.")
        if note.exists():
            return False
        if extension not in {".jpg", ".png", ".gif", ".webp", ".avif"}:
            raise bili.ImportProblem("Unsupported cover image.")
        self.prepare()
        cover = self.covers / f"{video.identifier}{extension}"
        content = self.render_note(video, cover).encode("utf-8")
        bili.atomic_write(cover, cover_data, replace=False)
        return bili.atomic_write(note, content, replace=False)


def load_libraries(inbox, config_path=None):
    inbox = Path(inbox).resolve()
    folders = {platform: inbox / platform for platform in PLATFORMS}
    if config_path is not None and Path(config_path).is_file():
        config_path = Path(config_path).resolve()
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        values = data.get("collections", {}) if isinstance(data, dict) else None
        if not isinstance(values, dict) or any(key not in PLATFORMS for key in values):
            raise ValueError("Invalid collection configuration.")
        for platform, value in values.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Collection folders must be non-empty paths.")
            folders[platform] = (config_path.parent / value).resolve()
    libraries = {platform: Library(root, platform) for platform, root in folders.items()}
    roots = [library.root for library in libraries.values()]
    if roots[0] == roots[1] or roots[0] in roots[1].parents or roots[1] in roots[0].parents:
        raise ValueError("Use separate, non-nested collection folders.")
    if len({library.vault for library in libraries.values()}) != 1:
        raise ValueError("Both collections must be in the same Obsidian vault.")
    return libraries
