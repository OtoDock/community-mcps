# video-tools — agent-driven video editing MCP (design)

> Design doc for the shipped implementation: the original architecture,
> landscape research, and the rationale behind the tool surface. The
> manifest, README and skills are the operational reference.

## Goal

An agent creates and edits professional video end-to-end: showcase/product videos,
music-synced clips, TikTok-style short-form with animated captions, motion graphics —
self-hosted, no cloud editing service, no desktop NLE required. Output quality target:
"a human would publish this", with the human doing the final full-speed watch.

## Positioning (landscape research 2026-07-06)

The slot is open. Mature agent-editing options either drive a paid desktop NLE
(`samuelgursky/davinci-resolve-mcp`, needs Resolve Studio) or a cloud account
(Shotstack/Opus Clip MCPs); the 2025 ffmpeg-wrapper MCPs were unmaintained as
of this research. Patterns adopted from the products that work:

- **Text is the timeline** (Descript): the model reasons over word-timestamped
  transcripts + structured metadata, never over pixels/audio directly.
- **Deterministic signal pass first, LLM second** (everyone): scenes/beats/loudness
  extracted by cheap non-LLM analysis; the model plans against the index.
- **Preflight validation + compound tools** (davinci-resolve-mcp, KyaniteLabs/mcp-video):
  validate before render, return actionable errors.
- **Plan → preview → approve → final** (HeyGen Video Agent); read/write tool split.
- **HTML-as-video for motion graphics** (HeyGen HyperFrames, Apache-2.0, 33k★ in 4 months):
  LLMs author HTML/CSS far better than any video DSL.
- **NLE handoff as escape hatch** (Eddie AI, auto-editor): export the cut for human
  finishing — deferred here (OTIO, P3), but the schema stays mappable.

## Architecture

Mirrors the platform's file-tools MCP — the proven stateless docker-sidecar
pattern, shared-pool-safe for T3 cloud:

- `runtime: docker`, `transport: http`, `StreamableHTTPSessionManager(stateless=True)`,
  `(session_id, auth)` bound into request-scoped contextvars.
- `proxy_callbacks: true` → per-session JWT bearer; **no master key in the container**.
- Files pass by path over the `${HOST_AGENTS_DIR}:/agents` volume; every incoming path
  goes through the proxy `resolve-path` hook (scope-aware, satellite-cache, NFC/NFD).
  On containerized (T2) installs the platform's compose rewrite maps this bind onto
  the shared `otodock-agents` named volume — the interpolated host path there is an
  empty dir, so the volume mapping is what makes agent files visible.
- Loopback-only port bind (the container RW-mounts the agents tree).
- `server.image` on GHCR, built by CI.
- **Explicit `mem_limit: 4g`** in the compose (chromium + ffmpeg; the injected 2g
  default would OOM renders). Explicit limits always win over the injected default.
- No session state in the container. Render temp lives under container-local
  `/tmp/render-<uuid>` and is deleted after the request; only final artifacts are
  written to `/agents` workspace paths.

**The artifact the agent edits is a declarative JSON composition** — a project file in
the user's workspace (`<name>.vproj.json`). The MCP compiles it to ffmpeg 8.x
filtergraphs + generated ASS files at render time. Rationale: LLMs author/revise
structured documents far more reliably than imperative ffmpeg command chains (why the
wrapper MCPs died); project state survives sessions, is human-inspectable, and keeps
the container stateless; small edits = small JSON ops + re-render (the `write_docx`
operation model).

**In-container engine stack** (all self-hosted, licence-clean):
ffmpeg 8.1+ (xfade / maskedmerge / alphamerge / chromakey / lut3d / curves / eq /
loudnorm), libass, PySceneDetect (BSD), librosa (ISC) for beats/BPM/energy/sections,
Pillow, headless Chromium + Playwright (motion clips), OpenCV-headless + a small face
detector (YuNet ONNX or MediaPipe — pick at impl) for P2 reframing. Baked font pack:
OFL faces (Inter, Montserrat, JetBrains Mono, …) + Noto for multilingual coverage
(incl. Greek); user fonts loadable from workspace. Image budget ~1.5–2 GB — fine on GHCR.

**Web-safe defaults**: every render outputs H.264/AAC MP4 `+faststart` unless asked
otherwise, so the proxy's playback pipeline (`display_video` → `/v1/hooks/media`)
passes it through without a second transcode.

## Composition schema v1 (sketch — final shape at impl)

```jsonc
{
  "version": 1,
  "project": { "width": 1080, "height": 1920, "fps": 30, "background": "#000" },
  "tracks": [                                   // bottom-up z-order
    { "kind": "video", "clips": [
      { "src": "captures/demo.mp4", "in": 12.4, "out": 21.9, "start": 0.0,
        "speed": 1.0,
        "transform": { "scale": 1.15, "pos": [0, -80],
                        "keyframes": [ { "t": 0, "scale": 1.0 }, { "t": 9.5, "scale": 1.15 } ] },
        "transition_in": { "type": "circleopen", "duration": 0.4 },   // xfade names
        "color": { "exposure": 0.1, "saturation": 1.06, "lut": "looks/teal-orange.cube" },
        "effects": [ { "type": "chromakey", "color": "#00ff00", "similarity": 0.12 } ] } ] },
    { "kind": "overlay", "clips": [
      { "motion": "titles/hook.html", "start": 0.2, "duration": 2.8, "alpha": true } ] },
    { "kind": "audio", "clips": [
      { "src": "music/track.wav", "start": 0, "gain_db": -6,
        "duck": { "against": "video", "amount_db": -10, "attack": 0.15 } } ] }
  ],
  "captions": { "source": "captures/demo.transcript.json", "preset": "karaoke-pop",
                 "position": "lower_third" },
  "audio_master": { "loudnorm": { "target_lufs": -14 } }
}
```

Kept conceptually mappable to OpenTimelineIO (tracks / clips / in-out) so a P3 OTIO
exporter is mechanical.

## Tool surface (v1)

Read / analysis:
- `probe_media` — ffprobe summary (streams, codecs, duration, fps, resolution).
- `analyze_video` — PySceneDetect shot list + one thumbnail per shot as a contact-sheet
  image; the model's persistent visual index, referenced by timestamp thereafter.
- `sample_frames` — frames at given timestamps from a source file (grid image).
- `analyze_audio` — BPM, beat-grid timestamps, energy curve, section estimates,
  silence map, R128 loudness + a rendered waveform PNG.

Write / composition:
- `create_composition` / `edit_composition` — operation-based edits on the project JSON.
- `validate_composition` — preflight (missing assets, overlaps, bad params) with
  actionable errors; also runs implicitly before any render.
- `render_composition` — `mode=preview` (low-res, optional time range, fast) or
  `mode=final`. Long finals: job pattern (`render_status`) vs one long call with MCP
  progress notifications — decide at impl; previews always synchronous.
- `render_frames` — stills from the *composition* (post-effects) at timestamps — the
  `screenshot_document` analog; the core QC loop.
- `edit_video` — one-shot ops without composition ceremony: trim, crop, resize, speed,
  concat, extract/replace audio, loudness-normalize, burn subtitles, gif/webp export.
- `render_motion_clip` — see below.

## How the model "sees" and "hears" (the skill teaches this loop)

analyze (shots + beats + transcript + waveform) → plan cuts as **beat-grid arithmetic**
(cut points snap to beat timestamps; montage sections placed by section labels) →
compose → preview render → `render_frames` at cut/transition points → adjust → final →
human watches inline (`display_video`) and gives notes. Color follows the file-tools
photo-editing philosophy: analyze first, conservative moves, looks via LUT — restraint
guide ships in the skill.

## Captions

Consume word-level transcript JSON / SRT / ASS from the workspace and burn via libass.
The platform's transcribe MCP emits `<stem>.transcript.json` (word timestamps) + karaoke
ASS — the natural producer on installs that have it. **No hard dependency**: video-tools
accepts caption sources from anywhere; styled presets (karaoke word-pop, Hormozi-style,
minimal lower-third) are applied here. Optional later: a built-in `transcribe` convenience that calls the proxy's
`/v1/audio/transcribe` hook with the per-session JWT (same endpoint transcribe-mcp uses).

## Looks / LUTs

Ship an **own** look library: recipes (curves/eq/colorbalance combinations) baked into
generated `.cube` files we fully own — zero redistribution risk. Accept user-supplied
`.cube` from the workspace (personally-licensed commercial LUTs). Third-party packs
(e.g. CC-BY-SA film-emulation HaldCLUT sets) only after per-pack licence vetting; most
"free LUTs" are free-to-use, NOT free-to-redistribute — do not bundle those.

## Motion graphics — `render_motion_clip`

Agent-authored HTML/CSS(+JS: WAAPI or bundled GSAP) rendered deterministically:
fresh Playwright context per render, animations paused, virtual clock stepped
`t = frame/fps` (Playwright clock API / `document.getAnimations()` seek /
`Emulation.setVirtualTimePolicy` — pick at impl), screenshot per frame
(`omitBackground: true` for alpha) → PNG sequence → encode. Outputs: MP4 (opaque),
WebM/VP9-alpha or PNG-seq (for composition overlays), GIF/WebP (standalone social
assets). General-purpose by design: results land in the workspace and are usable
outside any video project (animated logos, banners, post assets).

Security posture: per-render throwaway context, **network egress blocked** except
`file://`/`data:` and an in-container asset endpoint serving the mounted workspace
(no external fetch, no LAN probing from agent-authored JS); frame-count/duration caps.

## Out of scope v1 (companions)

- **Music/SFX sourcing & generation** — separate future music-gen MCP (ElevenLabs
  Music/SFX API first — licensed training data, commercial-safe; self-hosted Stable
  Audio 3.0 / ACE-Step later; Freesound/Openverse CC0/CC-BY fetch with licence-sidecar
  + auto-attribution). **Never** MusicGen (CC-BY-NC) or yt-dlp-ripped audio for
  published output (ToS breach, no licence trail, Content ID strikes).
- **OTIO export** (P3) — NLE handoff to Resolve/Premiere/FCP.
- **DaVinci Resolve app-connector** (P3, R2+) — wrap `samuelgursky/davinci-resolve-mcp`
  via the Blender `companion_app` pattern; needs paid Resolve Studio.
- **Auto-shorts pipeline** (P3) — long-form → scored clips (Opus-style explained
  rubric), rides on P1/P2 primitives.
- **Screen recording** — a platform capability, not this MCP.
- GPU/hardware encode — env knob later; CPU libx264 is the portable default.

## Phasing

- **P1 (core)**: analysis tools; schema + compiler (cut/trim/crop/scale/position/speed/
  concat, multi-track); xfade transitions; ASS caption burning + presets; audio tracks
  with gain/ducking + R128 master loudnorm; color ops + LUT; preview/final render;
  `render_frames`; `edit_video`; skill file.
- **P2 (pro layer)**: masks/chromakey/alpha compositing; keyframed transforms
  (Ken Burns); smart 16:9→9:16 reframe (face detect + track); `render_motion_clip`;
  look library.
- **P3 (later)**: OTIO export, Resolve connector, auto-shorts, music-gen MCP,
  hw-encode knob.
- **0.2.0 "pro" pack**:
  - *Stabilization* (`stab.py`): vidstab two-pass. The detect pass is a
    RENDERER pre-pass (`_prepare_stabilization`) — the compiler stays pure
    and only consumes an injected `_stab` field carrying a tmp-staged .trf
    path. Transform files are cached as sidecars next to the source
    (`<stem>.stab-<key>.trf`, key = size/mtime/span/shakiness) so preview,
    frames, and final reuse one analysis; staged copies keep
    user-controlled filenames out of the filtergraph. `vidstabtransform`
    sits between setpts and the fps resample: the .trf indexes source
    frames by ORDER, so any resample before it would misalign corrections.
    Also an `edit_video` op. Presets low/medium/high.
  - *Real slow motion* (`slowmo.py`): speed floor 0.1 with
    `interpolate: flow|blend|duplicate`. Native-first — src_fps × speed ≥
    timeline fps means retimed native frames fill every output frame (no
    synthesis, whatever the mode). `blend` is a pure-compiler inline
    minterpolate after the setpts stretch. `flow` (mci) pre-renders a
    MEZZANINE in the renderer: trim + stretch + stabilization (when
    present — stabilize-then-interpolate order matters) + interpolation
    baked at timeline fps, crf 10, cached in a bounded container-local
    LRU dir (`VIDEO_TOOLS_MEZZANINE_CACHE_GB`, default 4) — deliberately
    NOT a user-visible sidecar (100 MB intermediates through satellite
    file sync would hurt more than a re-render). Baking the stretch keeps
    minterpolate's target at timeline fps instead of a source-domain
    fps/speed monster (0.1× @ 30fps timeline = 300 fps). The graph
    consumes the mezzanine via `_slomo` like ordinary media; audio keeps
    the original source through atempo.
  - *Audio suite* (`audiofx.py`): per-clip `audio: {denoise, eq, compress,
    deess}` (fixed classic order) + master `audio_master.{eq, compress,
    limiter}` — pure filter builders. denoise true → afftdn with
    `nf=-30` seeding (the -50 default treats real hiss as signal and
    reduces NOTHING; tn=1 tracking also defeats it — both measured);
    denoise "voice" → arnndn with the bundled public-domain rnnoise
    model (`models/rnnoise-voice.rnnn`, −15 dB floor drop on the noisy-VO
    fixture). EQ via plain biquads (equalizer/bass/treble/highpass) —
    anequalizer would need per-channel band duplication. Master chain
    sits before the loudnorm token so pass 1 measures the processed bus;
    alimiter (level=disabled) is the true-peak safety for loudnorm:false.
    One-shot `edit_video` op `enhance_audio {preset: voice|music}`.
  - *Shot color matching* (`colormatch.py`): `color: {match: {ref:
    "path@t"}}` on base clips + `edit_video` op `match_color`. Per-RGB-
    channel QUANTILE-curve transfer (9 quantile pairs → monotone np.interp
    curve → separable .cube LUT via lut3d) — a Lab mean/std affine was
    measured to recover only ~25% of a channel-mixer tint (tints are
    multiplicative in RGB, additive transfer overshoots on saturated
    content); quantile curves recover ~94% and capture gamma/lift too.
    Renderer pre-pass samples frames (cv2, 3-frame window) and bakes tmp
    cubes; the match LUT applies BEFORE creative grades. Bridge mode
    `{ramp_from: "A@t", ramp_to: "B@t"}` bakes TWO LUTs and the compiler
    split/blends between the two grades across the clip
    (blend=all_expr with clip-local T) — the AI-bridge join approach,
    chosen over a concealing micro-crossfade; cuts stay hard.
    Assumes similar content between target and reference samples (true at
    junctions — documented in the skill).
  - *Filmic finishing*: `vignette`/`grain`/`sharpen` 0–1 knobs on base
    clips and the project (sharpen→grain→vignette after the grade), plus
    `project.letterbox` (drawbox bars over the composited frame, before
    captions so text sits on the bars). `speed_ramp` (segmented
    constant-speed MVP) deferred to the polish tier per the pro plan's
    ordering — after wow transitions.
  - *Wow transition presets* (`transitions.py`): whip_pan / zoom_punch /
    flash_cut / glitch / spin / shake. NOT xfades — edge treatments
    around a HARD CUT: D/2 styling on the outgoing tail + D/2 on the
    incoming head, injected into the clip chains (t is clip-local there)
    as enable-windowed filters (dblur/eq/rgbashift/noise/gblur steps) and
    zoompan/rotate expressions (shake decays zoom+jitter smoothly to
    exactly 1.0 — no end pop). compute_timeline gives presets ZERO
    overlap, so the fold concats and timeline math is untouched. Preset
    duration capped at 2.5s by validation; skill carries the taste rules
    (on a beat, 0.2–0.4s, sparingly). Field feedback: glitch + shake
    are the social first picks (skill says so);
    flash_cut/zoom_punch dip to BLACK by default ({flash: "white"} for
    the classic pop). KOLDER ZOOMS (zoom_in/zoom_out, cut
    presets): 2×2 mirror tile (pad+fillborders=mirror —
    Premiere's Motion Tile) + eased zoompan; ONE continuous motion across
    the cut (outgoing ease-IN so max speed lands ON the cut, incoming
    ease-OUT), mirrored phase gets 40% of the duration vs 60% clean.
    PREMIUM OVERLAPPING PRESETS: whip_left/whip_right = ADJACENT-STRIP
    camera whips via xfade custom expr (direction = camera pan; the next
    shot slides in attached on that side, 5%-width feathered seam,
    smoothstep ramp, dblur edge ramps on top) — NOTE xfade custom P runs
    1→0 (progress = 1-P) and st/ld RACE in per-pixel exprs (inline
    everything); luma_wipe = structural maskedmerge join (animated geq
    threshold on the outgoing luma, mask gray→gbrp so all channels wipe
    together — an xfade-custom version cast colors; every segment
    format-pinned or the gray constraint back-propagates and grayscales
    the timeline; fps= after the concat or a later xfade EINVALs). Plus
    clip-level motion_blur: tmix frame stacking (strongest on
    high-fps/flow footage) + an edit_video op.
  - *Speed ramps* (`speedramp.py`): `speed_ramp:
    {from, to, curve: linear|ease_in|ease_out}` on base media clips
    (replaces `speed`), compiled as 6 constant-speed sub-clips over equal
    SOURCE-time slices — speeds sampled at segment midpoints on the curve
    (rounded 4dp for stable cache keys). Expansion runs on the renderer's
    RESOLVED copy before the pre-passes (slow segments ride the flow
    mezzanine) and idempotently inside compile_render. transition_in/
    fade_in stay on the first segment, fade_out on the last; validation
    rejects transform.keyframes and color.match combos (segmentation
    would restart both) and gives neighbor transitions only the ramp's
    EDGE SEGMENT (1/6) of room. Plus a one-shot edit_video `speed_ramp`
    op (duplicate-frame quick path). Ramped audio steps per segment —
    skill says mute + music below 0.5×.
  - *VFR duration pin* (compiler — found in a full-length dress
    rehearsal): trimmed spans of VFR sources decode SHORT of
    span/speed (last pts < out; slow motion divides the deficit by the
    speed) — a 6-segment ramp came out 0.8s short, xfade offsets fired
    late, the final fade truncated, and the sample-exact audio drifted
    ~1s. Every media chain (base + overlay) now pins to its computed
    duration after the fps CFR conversion: tpad=stop_mode=clone +
    trim=end=<computed> + re-asserted fps= (the trim clears the CFR rate
    metadata xfade requires). True-VFR execution regression included.
  - *Caption cue pacing*: _CUE_GAP_SPLIT 0.8→0.45s — a 0.79s phrase
    pause must break a karaoke cue (within-phrase gaps are <0.3s).
  - *Validation*: stress matrix complete (incl. live native-60fps
    retiming) + two full dress rehearsals reviewed (cinematic montage;
    beat-synced 9:16 social cut with TTS VO + karaoke captions). The
    review added two SKILL taste rules: zoom presets need different
    scenes on the two sides (same-scene = jump-cut read; whips/glitch
    hide same-scene jumps), and faceless subjects need a
    subject-position check + crop bias (smart_reframe center-crops
    without faces) until object tracking lands.

## Low-RAM windowed rendering

A composition renders as ONE ffmpeg filtergraph, and ffmpeg's scheduler lets
decoded frames pile up in unbounded filtergraph queues at the fold/overlay
junctions: peak RSS ≈ every decoded SOURCE frame of the render window at
once. Measured on a real 70 s 1080p30 timeline (24 base clips + 10
overlays): 12.7 GB anon-rss → OOM-killed a 15 GB host; the planner's
decode estimate for the same timeline is 14.1 GB. Toy-scale experiments
confirmed the shape: a 72 s 12-clip fold at 320x180 buffers exactly
timeline × fps × frame_size (+190 MB over a 70 MB floor), the pile does NOT
scale with the render canvas (it is source-side, pre-scale), and a 6 s
`time_range` slice used to cost the same as the full render because nothing
was pruned.

The fix (renderer + compiler, `plan_segments`/`window_pruned`/
`estimate_window_bytes`/`bare_cut_points`):

- **Windowed final render.** When the whole-timeline decode estimate
  exceeds the memory budget (default 40% of physical RAM, cgroup-aware;
  `VIDEO_TOOLS_RENDER_BUDGET_MB` overrides), the timeline renders in
  windows and the windows concat losslessly (`-c copy`) and mux with the
  audio. Bare cuts are the preferred (free) split points; a span between
  adjacent bare cuts that alone busts the budget gains SYNTHETIC frame-grid
  points inside it (`MIN_WINDOW` 4 s floor, `MAX_SEGMENTS` backstop), so a
  single long continuous take or two long clips joined by an xfade window
  like anything else. Synthetic points never land within `SPLIT_GUARD` of a
  transition's overlap — the blend keeps real footage on both sides.
- **Window pruning.** Each window compiles from a sub-composition where
  out-of-window base clips become fills of IDENTICAL duration (timeline
  math, fold structure and transition offsets stay bit-identical — the
  substituted spans are trimmed away) and out-of-window overlays are
  dropped, so far media is never opened or decoded. A plain media clip that
  only PARTIALLY overlaps the window (the long-take case) is reduced to
  fill + sub-clip (+ fill) with the same total duration: the sub-clip's
  in-point advances onto the window and its input opens with a
  decode-accurate `-ss` (`_seek`, trims rebased), so the source decodes
  only the window plus a small preroll — not from zero. Sides shorter than
  `MIN_TRIM` stay untrimmed; stateful clips (speed ≠ 1, flow/blend
  interpolation, `_slomo`, `_stab`, `_match`, transform keyframes) always
  keep the whole-clip decode — a single live branch streams, so they are
  RAM-safe, just not decode-pruned. Every window recomputes the same
  timeline, so frame pts partition exactly at the edges: the concat is
  frame-exact, cuts stay cuts.
- **Audio renders once.** One full-timeline audio-only pass (frames are
  ~KB) keeps amix, sidechain ducking and two-pass loudnorm semantics
  identical to a single-pass render.
- **`time_range` and `render_frames` prune too**, via the same
  `window_pruned` (with a pad covering transition/styling reach) — a slice
  or QC frame no longer opens the whole timeline. The final-trim tail also
  re-pins `fps=`: trim clears the CFR metadata and the sink silently
  resampled slices to 25 fps.
- **Limits.** Only a span whose MIN_WINDOW-sized pieces still bust the
  budget renders as one window (pathological resolutions); the render
  warns that the container's memory cap may kill it. Project-level `grain`
  is temporally random, so its pattern reseeds at window edges (invisible
  in practice). Stateful clips keep whole-clip decode (see above): their
  decode CPU is unpruned, never their RAM.

## Open questions (decide at impl)

- Final-render delivery: async job + `render_status` vs long synchronous call with
  progress notifications (does the CLI surface MCP progress?).
- Port allocation (unique among docker MCPs; file-tools holds 8932/9981).
- Face detector choice (YuNet ONNX vs MediaPipe) and whether P2 reframe needs tracking
  (ByteTrack) or per-shot static crop (AutoFlip-style) suffices.
- Schema versioning / migration story for `.vproj.json` (pre-1.0: break freely).
- Whether `analyze_video` caches its index next to the source (`<stem>.analysis.json`)
  to avoid re-detection across sessions (leaning yes — mirrors transcript sidecars).
