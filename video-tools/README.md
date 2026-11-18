# Video Tools

Agent-driven video editing for OtoDock. The agent authors a declarative
composition file (`<name>.vproj.json`). It holds clips, transitions,
overlays, music with ducking, word-level animated captions and color grades.
This MCP compiles it to an FFmpeg filtergraph and renders web-safe MP4.

| Field | Value |
|-------|-------|
| Upstream project | [OtoDock](https://github.com/OtoDock/community-mcps/tree/main/video-tools) |
| Icon | the OtoDock mark (OtoDock wrote this MCP) |

## Tools

| Tool | What |
|---|---|
| `probe_media` | Container/stream facts for any media file |
| `analyze_video` | Shot detection + labeled contact sheet (cached in a file beside the source) |
| `analyze_audio` | BPM + beat grid, energy, silences, LUFS, waveform image |
| `sample_frames` | Frames from a source file at timestamps (grid image) |
| `align_audio` | Offset between two recordings of one event (camera vs recorder, two cameras), with the implied placement checked for plausibility |
| `create_composition` / `edit_composition` / `validate_composition` | Author the timeline. `format=json` gives the computed timeline as data |
| `render_composition` | preview (fast) / final (crf 18 + two-pass R128 loudnorm). `background=true` answers at once with a job |
| `render_status` / `cancel_render` | How far a background render is and its report when done, or stop it |
| `render_filmstrip` | One small picture of a clip's frames, for a timeline's thumbnails |
| `render_frames` | Frames from the composition: the visual QC loop |
| `edit_video` | One-shot ops: trim/remove/crop/rotate/resize/speed/concat/audio/subtitles/GIF |

**`render_composition`** runs the final render's loudnorm in `auto`,
`linear` or `dynamic` mode. The result reports which mode ran and the
loudness measured on the encoded file. A render writes beside its output
under a hidden name and takes the output's name only when it succeeded.
With `background=true` the call checks the composition and starts the
render, and `render_status` gives its progress and its report. A
background render lives as long as the tool runs: after a restart its
status is `lost`. While a render runs, OtoDock keeps the tool awake for up
to an hour.

**`edit_video`** tags its re-encoded outputs Rec.709. HLG/PQ sources pass
through untouched.

## Design

- **Separate Docker container.** Mirrors file-tools: a per-session JWT via
  proxy callbacks and a loopback-only port. Every path goes through the
  proxy's `resolve-path` hook for the calling session, the container's own
  form too, and a write as a write. The `/agents` volume is the only file
  transport. Project state lives in the workspace. Only a
  background render's progress lives in the container, until it restarts.
- **The model never watches video.** Deterministic analysis (PySceneDetect,
  librosa beat tracking, EBU R128) turns media into timestamped structure.
  Beat-syncing is arithmetic against the grid. QC is rendered frames.
- **Captions** consume word-timestamped transcript JSON (or SRT/ASS) and
  burn styled ASS via libass. The styles are karaoke word-fill, one-word
  pop, broadcast clean and minimal boxed. Captions use a shipped family
  (Inter, Roboto, Comfortaa, Montserrat, Noto, …) or any `.ttf`/`.otf` from
  the workspace.
- **Audio that stays in sync.** The sweetening chain (denoise, EQ,
  compression, de-ess, leveller) is latency-neutral. The denoisers' 10/25 ms
  processing delay is compensated inside it. Per-clip gain automation and
  fades shape levels without splitting clips. Loudness normalization is
  explicit:
  - `auto` reports (and warns) when ffmpeg's two-pass loudnorm fell back to
    its dynamic compressor.
  - `linear` applies one static gain plus a true-peak limiter and keeps the
    mix's dynamics.
- **Looks** are recipe-generated `.cube` LUTs owned by this repo. They are
  baked at image build, and nothing third-party is redistributed. User
  `.cube` files work by path, chain in order, and take a per-LUT strength.
- **Colour-managed timeline.** The timeline is Rec.709/limited:
  - Sources declare their colorimetry (`color.input`).
  - HLG and PQ (HDR10) footage converts with built-in presets
    (`color.convert: "hlg->rec709"` / `"pq->rec709"`), and needs no
    technical LUT.
  - Renders carry a truthful VUI + `colr`.

  `probe_media` tells you what a file carries and which preset it needs.
- Output defaults to H.264/AAC MP4 with `+faststart`. It plays inline in the
  dashboard with zero re-transcoding.

## Development

```
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt pytest
.venv/bin/python -m pytest __tests__ -q
```

Execution tests need an `ffmpeg`/`ffprobe` on PATH (or `FFMPEG_PATH`/
`FFPROBE_PATH`). They synthesize their own media. Lockfile changes:
`uv pip compile requirements.in -o requirements.txt`.
