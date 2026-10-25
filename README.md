# Video to Obsidian

A local tool for collecting Bilibili and YouTube videos in Obsidian. Import links as Markdown notes with local covers, durations, tags, and viewing progress, then browse them in native Bases galleries.

Videos play on their original platforms. Duplicate imports preserve existing notes.

Imports individual videos with publicly accessible metadata and a known duration. Playlist imports and sign-in-only videos are not supported.

## Usage

Requires **Python 3.10+** and **Obsidian with Bases enabled**. No extra Python packages or API keys.

1. Place `video-to-obsidian` inside your vault.
2. Run `start.bat` on Windows, or `python app.py`.
3. Paste Bilibili or YouTube links and click **Import**. Use **Gallery** to open either collection.

The tool creates sibling `bilibili` and `youtube` folders, each containing `videos/`, `covers/`, and `Gallery.base`. Failed links remain in the input for retrying.

Input clears on refresh or close and is not saved as a draft. Older browser drafts are removed when the page opens.

In a note, set `watched_time` to `01:01` or `01:01:01`; reaching the duration marks it **Watched**. Add tags and write under **Notes**.

For custom collection paths, copy `config.example.json` to `config.json` and edit the paths relative to that file.

## Project structure

```text
video-to-obsidian/
├── app.py               # Local server and import jobs
├── index.html           # Web interface
├── catalog.py           # Link detection and provider routing
├── bilibili.py          # Bilibili metadata and covers
├── youtube.py           # YouTube metadata and covers
├── library.py           # Notes and cover storage
├── desktop.py           # Obsidian window handling
├── templates/           # Bases gallery template
├── tests/               # Regression tests
├── start.bat            # Windows launcher
├── LICENSE              # MIT license
└── config.example.json  # Collection path settings
```

## License

Licensed under the [MIT License](LICENSE).
