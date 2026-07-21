# Video Tools

Agent-driven video editing for OtoDock: the agent authors a declarative
composition file (`<name>.vproj.json`) — clips, transitions, overlays, music
with ducking, word-level animated captions, color grades — and this MCP
compiles it to an FFmpeg filtergraph and renders web-safe MP4.

## Tools

| Tool | What |
|---|---|
| `probe_media` | Container/stream facts for any media file |
| `analyze_video` | Shot detection + labeled contact sheet (cached sidecar) |
| `analyze_audio` | BPM + beat grid, energy, silences, LUFS, waveform image |
| `sample_frames` | Frames from a source file at timestamps (grid image) |
| `create_composition` / `edit_composition` / `validate_composition` | Author the timeline |
| `render_composition` | preview (fast) / final (crf 18 + two-pass R128 loudnorm) |
| `render_frames` | Frames from the composition — the visual QC loop |
| `edit_video` | One-shot ops: trim/remove/crop/resize/speed/concat/audio/subtitles/GIF |

## Design

- **Docker sidecar, stateless.** Mirrors file-tools: per-session JWT via
  proxy callbacks, paths resolved through the proxy's `resolve-path` hook,
  the `/agents` volume as the only file transport, loopback-only port.
  Project state lives in the workspace, never in the container.
- **The model never watches video.** Deterministic analysis (PySceneDetect,
  librosa beat tracking, EBU R128) turns media into timestamped structure;
  beat-syncing is arithmetic against the grid; QC is rendered frames.
- **Captions** consume word-timestamped transcript JSON (or SRT/ASS) and
  burn styled ASS via libass — karaoke word-fill, one-word pop, broadcast
  clean, minimal boxed.
- **Looks** are recipe-generated `.cube` LUTs owned by this repo (baked at
  image build — nothing third-party redistributed); user `.cube` files work
  by path.
- Output defaults to H.264/AAC MP4 with `+faststart` — plays inline in the
  dashboard with zero re-transcoding.

## Development

```
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt pytest
.venv/bin/python -m pytest __tests__ -q
```

Execution tests need an `ffmpeg`/`ffprobe` on PATH (or `FFMPEG_PATH`/
`FFPROBE_PATH`); they synthesize their own media. Lockfile changes:
`uv pip compile requirements.in -o requirements.txt`.
