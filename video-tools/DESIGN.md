# video-tools: agent-driven video editing MCP (design)

> Design doc for the shipped implementation: the original architecture,
> landscape research, and the rationale behind the tool surface. The
> manifest, README and skills are the operational reference.

## Goal

An agent creates and edits professional video end-to-end: showcase/product videos,
music-synced clips, TikTok-style short-form with animated captions, motion graphics.
Everything is self-hosted, with no cloud editing service and no desktop NLE required.
Output quality target: "a human would publish this", with the human doing the final
full-speed watch.

## Positioning (landscape research 2026-07-06)

The slot is open. Mature agent-editing options either drive a paid desktop NLE
(`samuelgursky/davinci-resolve-mcp`, needs Resolve Studio) or a cloud account
(Shotstack/Opus Clip MCPs). The 2025 ffmpeg-wrapper MCPs were unmaintained as
of this research. Patterns adopted from the products that work:

- **Text is the timeline** (Descript): the model reasons over word-timestamped
  transcripts + structured metadata, never over pixels/audio directly.
- **Deterministic signal pass first, LLM second** (everyone): scenes/beats/loudness
  extracted by cheap non-LLM analysis. The model plans against the index.
- **Preflight validation + compound tools** (davinci-resolve-mcp, KyaniteLabs/mcp-video):
  validate before render, return actionable errors.
- **Plan, preview, approve, final** (HeyGen Video Agent), and a read/write tool split.
- **HTML-as-video for motion graphics** (HeyGen HyperFrames, Apache-2.0, 33k stars in 4
  months): LLMs author HTML/CSS far better than any video DSL.
- **NLE handoff as escape hatch** (Eddie AI, auto-editor): export the cut for human
  finishing. This is deferred here (OTIO, P3), but the schema stays mappable.

## Architecture

Mirrors the platform's file-tools MCP, the proven stateless docker-sidecar
pattern, shared-pool-safe for T3 cloud:

- `runtime: docker`, `transport: http`, `StreamableHTTPSessionManager(stateless=True)`,
  `(session_id, auth)` bound into request-scoped contextvars.
- `proxy_callbacks: true` gives a per-session JWT bearer. **No master key in the
  container**.
- Files pass by path over the `${HOST_AGENTS_DIR}:/agents` volume. Every incoming path
  goes through the proxy `resolve-path` hook (scope-aware, satellite-cache, NFC/NFD).
  On containerized (T2) installs the platform's compose rewrite maps this bind onto
  the shared `otodock-agents` named volume. The interpolated host path there is an
  empty dir, so the volume mapping is what makes agent files visible.
- Loopback-only port bind (the container RW-mounts the agents tree).
- `server.image` on GHCR, built by CI.
- **Explicit `mem_limit: 4g`** in the compose, for chromium + ffmpeg. The injected 2g
  default would OOM renders. Explicit limits always win over the injected default.
- No session state in the container. Render temp lives under container-local
  `/tmp/render-<uuid>` and is deleted after the request. Only final artifacts are
  written to `/agents` workspace paths.

**The artifact the agent edits is a declarative JSON composition**: a project file in
the user's workspace (`<name>.vproj.json`). The MCP compiles it to ffmpeg 7.x
filtergraphs + generated ASS files at render time. Rationale:

- LLMs author/revise structured documents far more reliably than imperative ffmpeg
  command chains (why the wrapper MCPs died).
- Project state survives sessions, is human-inspectable, and keeps the container
  stateless.
- Small edits = small JSON ops + re-render (the `write_docx` operation model).

**In-container engine stack** (all self-hosted, licence-clean):

- ffmpeg 7.x, the image's Debian package (xfade / maskedmerge / alphamerge / chromakey /
  lut3d / curves / eq / loudnorm)
- libass
- PySceneDetect (BSD)
- librosa (ISC) for beats/BPM/energy/sections
- Pillow
- headless Chromium + Playwright (motion clips)
- OpenCV-headless + a small face detector (YuNet ONNX or MediaPipe, pick at impl) for
  P2 reframing

Baked font pack: OFL faces (Inter, Montserrat, JetBrains Mono, …) + Noto for
multilingual coverage (incl. Greek). User fonts are loadable from the workspace.
Image budget ~1.5 to 2 GB, fine on GHCR.

**Web-safe defaults**: every render outputs H.264/AAC MP4 `+faststart` unless asked
otherwise. So the proxy's playback pipeline (`display_video` to `/v1/hooks/media`)
passes it through without a second transcode.

## Composition schema v1 (sketch: final shape at impl)

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
- `probe_media`: ffprobe summary (streams, codecs, duration, fps, resolution).
- `analyze_video`: PySceneDetect shot list + one thumbnail per shot as a contact-sheet
  image. This is the model's persistent visual index, referenced by timestamp
  thereafter.
- `sample_frames`: frames at given timestamps from a source file (grid image).
- `analyze_audio`: BPM, beat-grid timestamps, energy curve, section estimates,
  silence map, R128 loudness + a rendered waveform PNG.

Write / composition:
- `create_composition` / `edit_composition`: operation-based edits on the project JSON.
- `validate_composition`: preflight (missing assets, overlaps, bad params) with
  actionable errors. It also runs implicitly before any render.
- `render_composition`: `mode=preview` (low-res, optional time range, fast) or
  `mode=final`. A long final runs in the background (`background: true`, then
  `render_status` and `cancel_render`). A preview runs in the call.
- `render_frames`: stills from the *composition* (post-effects) at timestamps. It is
  the `screenshot_document` analog, and the core QC loop.
- `edit_video`: one-shot ops without composition ceremony (trim, crop, resize, speed,
  concat, extract/replace audio, loudness-normalize, burn subtitles, gif/webp export).
- `render_motion_clip`: see below.

## How the model "sees" and "hears" (the skill teaches this loop)

1. Analyze (shots + beats + transcript + waveform).
2. Plan cuts as **beat-grid arithmetic**. Cut points snap to beat timestamps, and
   montage sections are placed by section labels.
3. Compose.
4. Preview render.
5. `render_frames` at cut/transition points.
6. Adjust.
7. Final.
8. The human watches inline (`display_video`) and gives notes.

Color follows the file-tools photo-editing philosophy: analyze first, conservative
moves, looks via LUT. A restraint guide ships in the skill.

## Captions

Consume word-level transcript JSON / SRT / ASS from the workspace and burn via libass.
The platform's transcribe MCP emits `<stem>.transcript.json` (word timestamps) + karaoke
ASS. It is the natural producer on installs that have it.

**No hard dependency**: video-tools accepts caption sources from anywhere. Styled
presets (karaoke word-pop, Hormozi-style, minimal lower-third) are applied here.
Optional later: a built-in `transcribe` convenience that calls the proxy's
`/v1/audio/transcribe` hook with the per-session JWT (same endpoint transcribe-mcp uses).

## Looks / LUTs

Ship an **own** look library: recipes (curves/eq/colorbalance combinations) baked into
generated `.cube` files we fully own, with zero redistribution risk. Accept
user-supplied `.cube` from the workspace (personally-licensed commercial LUTs).
Third-party packs (e.g. CC-BY-SA film-emulation HaldCLUT sets) only after per-pack
licence vetting. Most "free LUTs" are free-to-use, NOT free-to-redistribute: do not
bundle those.

## Motion graphics: `render_motion_clip`

Agent-authored HTML/CSS(+JS: WAAPI or bundled GSAP) rendered deterministically:

- fresh Playwright context per render, animations paused
- virtual clock stepped `t = frame/fps` (Playwright clock API /
  `document.getAnimations()` seek / `Emulation.setVirtualTimePolicy`, pick at impl)
- screenshot per frame (`omitBackground: true` for alpha), then PNG sequence, then
  encode

Outputs: MP4 (opaque), WebM/VP9-alpha or PNG-seq (for composition overlays), GIF/WebP
(standalone social assets). General-purpose by design: results land in the workspace
and are usable outside any video project (animated logos, banners, post assets).

Security posture:

- per-render throwaway context
- **The page reaches no network.** It loads from a routed origin,
  `https://motion.invalid/`. Its route serves the page, `data:`, `blob:` and
  `about:` URLs and the files the session may read, read beneath the mount. It
  refuses every other request. Service workers are blocked, and every WebSocket
  is refused.
- **A request the route does not see finds no network.** Chromium starts with
  a proxy on a loopback port where nothing listens, loopback addresses
  included. It resolves no name, and WebRTC sends nothing outside that proxy.
  This rests on Chromium's own settings. Its sandbox is off in the container,
  and the container keeps its network, so a fault in the browser itself is
  outside this guard.
- **A page takes a share.** One capture is served at most 64 names and 256 MB,
  one request at a time, and the answer says when a limit was reached. Two
  captures run at once, and a call that gets no turn within its time says so.
- frame-count/duration caps

Authoring contract (0.3.2, 2026-08):

- The tool runs PLATFORM-side, so `file://` references in the document are localized
  through `_resolve_path` before load. Use workspace paths.
- Any unresolvable ref is a hard preflight error listing every offender.
  Historically these failed silently: broken images, fallback fonts, a "successful"
  capture.
- A rewrite into a tmp copy injects `<base href>` to the original directory, so
  relative refs keep working. Failed sub-resource loads are surfaced as a WARNING in
  the result.
- Timing: capture starts after a `SETTLE_SECONDS` (1.0 s) virtual-clock settle.
  `window.VT_T0` (ms) is injected before page scripts run. `<html>` gains class
  `vt-capture`, and a `vt:capture-start` event fires at frame 0.
- JS timelines anchor `t = (Date.now() - (window.VT_T0 ?? 0)) / 1000`. On
  clock-install failure VT_T0 re-anchors to the real clock at capture start.
- CSS/WAAPI animations are seeked 0-based per frame and need no anchor.
- Stills (`render_still`) run 0-anchored with no settle.

## Out of scope v1 (companions)

- **Music/SFX sourcing & generation**: a separate future music-gen MCP. ElevenLabs
  Music/SFX API first (licensed training data, commercial-safe). Self-hosted Stable
  Audio 3.0 / ACE-Step later. Freesound/Openverse CC0/CC-BY fetch with
  licence-sidecar + auto-attribution. **Never** MusicGen (CC-BY-NC) or yt-dlp-ripped
  audio for published output (ToS breach, no licence trail, Content ID strikes).
- **OTIO export** (P3): NLE handoff to Resolve/Premiere/FCP.
- **DaVinci Resolve app-connector** (P3, R2+): wrap `samuelgursky/davinci-resolve-mcp`
  via the Blender `companion_app` pattern. It needs paid Resolve Studio.
- **Auto-shorts pipeline** (P3): long-form to scored clips (Opus-style explained
  rubric). It rides on P1/P2 primitives.
- **Screen recording**: a platform capability, not this MCP.
- GPU/hardware encode: env knob later. CPU libx264 is the portable default.

## Phasing

- **P1 (core)**:
  - analysis tools
  - schema + compiler (cut/trim/crop/scale/position/speed/concat, multi-track)
  - xfade transitions
  - ASS caption burning + presets
  - audio tracks with gain/ducking + R128 master loudnorm
  - color ops + LUT
  - preview/final render
  - `render_frames`
  - `edit_video`
  - skill file
- **P2 (pro layer)**:
  - masks/chromakey/alpha compositing
  - keyframed transforms (Ken Burns)
  - smart 16:9 to 9:16 reframe (face detect + track)
  - `render_motion_clip`
  - look library
- **P3 (later)**: OTIO export, Resolve connector, auto-shorts, music-gen MCP,
  hw-encode knob.
- **0.2.0 "pro" pack**:
  - *Stabilization* (`stab.py`): vidstab two-pass. The detect pass is a
    RENDERER pre-pass (`_prepare_stabilization`). The compiler stays pure
    and only consumes an injected `_stab` field carrying a tmp-staged .trf
    path.

    Transform files are cached as sidecars next to the source
    (`<stem>.stab-<key>.trf`, key = size/mtime/span/shakiness). So preview,
    frames, and final reuse one analysis. Staged copies keep
    user-controlled filenames out of the filtergraph.

    `vidstabtransform` sits between setpts and the fps resample: the .trf
    indexes source frames by ORDER, so any resample before it would
    misalign corrections. Also an `edit_video` op. Presets low/medium/high.
  - *Real slow motion* (`slowmo.py`): speed floor 0.1 with
    `interpolate: flow|blend|duplicate`. Native-first: src_fps × speed ≥
    timeline fps means retimed native frames fill every output frame (no
    synthesis, whatever the mode). `blend` is a pure-compiler inline
    minterpolate after the setpts stretch.

    `flow` (mci) pre-renders a MEZZANINE in the renderer: trim + stretch +
    stabilization (when present) + interpolation, baked at timeline fps,
    crf 10. The stabilize-then-interpolate order matters. The mezzanine is
    cached in a bounded container-local LRU dir
    (`VIDEO_TOOLS_MEZZANINE_CACHE_GB`, default 4). It is deliberately NOT a
    user-visible sidecar: 100 MB intermediates through satellite file sync
    would hurt more than a re-render.

    Baking the stretch keeps minterpolate's target at timeline fps instead
    of a source-domain fps/speed monster (0.1× @ 30fps timeline = 300 fps).
    The graph consumes the mezzanine via `_slomo` like ordinary media.
    Audio keeps the original source through atempo.
  - *Audio suite* (`audiofx.py`): per-clip `audio: {denoise, eq, compress,
    deess}` (fixed classic order) + master `audio_master.{eq, compress,
    limiter}`, all pure filter builders.

    With denoise true, afftdn runs with `nf=-30` seeding. The -50 default
    treats real hiss as signal and reduces NOTHING, and tn=1 tracking also
    defeats it (both measured). With denoise "voice", arnndn runs with the
    bundled public-domain rnnoise model (`models/rnnoise-voice.rnnn`,
    −15 dB floor drop on the noisy-VO fixture).

    EQ via plain biquads (equalizer/bass/treble/highpass): anequalizer
    would need per-channel band duplication. Master chain sits before the
    loudnorm token, so pass 1 measures the processed bus. alimiter
    (level=disabled) is the true-peak safety for loudnorm:false. One-shot
    `edit_video` op `enhance_audio {preset: voice|music}`.
  - *Shot color matching* (`colormatch.py`): `color: {match: {ref:
    "path@t"}}` on base clips + `edit_video` op `match_color`.
    Per-RGB-channel QUANTILE-curve transfer: 9 quantile pairs, then a
    monotone np.interp curve, then a separable .cube LUT via lut3d.

    A Lab mean/std affine was measured to recover only ~25% of a
    channel-mixer tint. Tints are multiplicative in RGB, and additive
    transfer overshoots on saturated content. Quantile curves recover
    ~94% and capture gamma/lift too.

    Renderer pre-pass samples frames and bakes tmp cubes. It samples a
    3-frame window, through ffmpeg with the clip's head tag since 0.4.1
    (cv2 read untagged HD through 601). The match LUT applies BEFORE
    creative grades.

    Bridge mode `{ramp_from: "A@t", ramp_to: "B@t"}` bakes TWO LUTs. The
    compiler split/blends between the two grades across the clip
    (blend=all_expr with clip-local T). This is the AI-bridge join
    approach, chosen over a concealing micro-crossfade. Cuts stay hard.
    Matching assumes similar content between target and reference samples
    (true at junctions, documented in the skill).
  - *Filmic finishing*: `vignette`/`grain`/`sharpen` 0 to 1 knobs on base
    clips and the project (sharpen, then grain, then vignette, after the
    grade). Plus `project.letterbox`: drawbox bars over the composited
    frame, before captions so text sits on the bars. `speed_ramp`
    (segmented constant-speed MVP) deferred to the polish tier per the pro
    plan's ordering, after wow transitions.
  - *Wow transition presets* (`transitions.py`): whip_pan / zoom_punch /
    flash_cut / glitch / spin / shake. These are NOT xfades but edge
    treatments around a HARD CUT: D/2 styling on the outgoing tail + D/2 on
    the incoming head. They are injected into the clip chains (t is
    clip-local there) as enable-windowed filters
    (dblur/eq/rgbashift/noise/gblur steps) and zoompan/rotate expressions.
    shake decays zoom+jitter smoothly to exactly 1.0, with no end pop.

    compute_timeline gives presets ZERO overlap, so the fold concats and
    timeline math is untouched. Preset duration is capped at 2.5s by
    validation. The skill carries the taste rules (on a beat, 0.2 to 0.4s,
    sparingly). Field feedback: glitch + shake are the social first picks
    (the skill says so). flash_cut/zoom_punch dip to BLACK by default
    ({flash: "white"} for the classic pop).

    KOLDER ZOOMS (zoom_in/zoom_out, cut presets): 2×2 mirror tile
    (pad+fillborders=mirror, Premiere's Motion Tile) + eased zoompan. ONE
    continuous motion across the cut: outgoing ease-IN so max speed lands
    ON the cut, incoming ease-OUT. The mirrored phase gets 40% of the
    duration vs 60% clean.

    PREMIUM OVERLAPPING PRESETS:

    - whip_left/whip_right = ADJACENT-STRIP camera whips via xfade custom
      expr. The direction = camera pan: the next shot slides in attached on
      that side. The join has a 5%-width feathered seam, a smoothstep ramp,
      and dblur edge ramps on top. NOTE: xfade custom P runs from 1 to 0
      (progress = 1-P), and st/ld RACE in per-pixel exprs (inline
      everything).
    - luma_wipe = structural maskedmerge join: an animated geq threshold on
      the outgoing luma. The mask converts gray to gbrp so all channels wipe
      together. An xfade-custom version cast colors. Every segment is
      format-pinned, or the gray constraint back-propagates and grayscales
      the timeline. fps= goes after the concat, or a later xfade EINVALs.

    Plus clip-level motion_blur: tmix frame stacking (strongest on
    high-fps/flow footage) + an edit_video op.
  - *Speed ramps* (`speedramp.py`): `speed_ramp:
    {from, to, curve: linear|ease_in|ease_out}` on base media clips
    (replaces `speed`). A ramp compiles as 6 constant-speed sub-clips over
    equal SOURCE-time slices. Speeds are sampled at segment midpoints on
    the curve (rounded 4dp for stable cache keys). Expansion runs on the
    renderer's RESOLVED copy before the pre-passes (slow segments ride the
    flow mezzanine), and idempotently inside compile_render.

    transition_in/fade_in stay on the first segment, fade_out on the last.
    Validation rejects transform.keyframes and color.match combos
    (segmentation would restart both). It also gives neighbor transitions
    only the ramp's EDGE SEGMENT (1/6) of room. Plus a one-shot edit_video
    `speed_ramp` op (duplicate-frame quick path). Ramped audio steps per
    segment: the skill says mute + music below 0.5×.
  - *VFR duration pin* (compiler, found in a full-length dress rehearsal):
    trimmed spans of VFR sources decode SHORT of span/speed. The last
    pts < out, and slow motion divides the deficit by the speed. A 6-segment
    ramp came out 0.8s short, xfade offsets fired late, the final fade
    truncated, and the sample-exact audio drifted ~1s.

    Every media chain (base + overlay) now pins to its computed duration
    after the fps CFR conversion: tpad=stop_mode=clone +
    trim=end=<computed> + re-asserted fps=. The trim clears the CFR rate
    metadata xfade requires. True-VFR execution regression included.
  - *Caption cue pacing*: _CUE_GAP_SPLIT moved from 0.8 to 0.45s. A 0.79s
    phrase pause must break a karaoke cue (within-phrase gaps are <0.3s).
  - *Validation*: stress matrix complete (incl. live native-60fps
    retiming). Two full dress rehearsals were reviewed: a cinematic
    montage, and a beat-synced 9:16 social cut with TTS VO + karaoke
    captions.

    The review added two SKILL taste rules. Zoom presets need different
    scenes on the two sides (same-scene = jump-cut read, while whips/glitch
    hide same-scene jumps). Faceless subjects need a subject-position
    check + crop bias (smart_reframe center-crops without faces) until
    object tracking lands.
- **0.3.0 tracking pack (2026-07-22, `track.py`)**:
  - `track_object` tool (open tier): frame-by-frame span tracking (cv2),
    median-5 + EMA path smoothing. It emits ready-to-paste
    `transform.keyframes` for tracked callouts/labels on overlays. The
    keyframes hold the subject-center offset from the FRAME center in
    source px, with span-relative t. Spans must stay within one shot (a cut
    loses the target, and the result says where).
  - `smart_reframe {track_box, track_start?, smoothness?}`:
    subject-FOLLOWING crop covering the WHOLE clip. Tracking runs backward
    AND forward from wherever the box was drawn (`track_span_full`).

    The crop path comes from a DEADZONE controller. The crop stays
    perfectly still while the subject sits near center, and glides
    proportionally past the deadzone. A hard safety pull guarantees the
    subject center never leaves the crop's central 80%. `smoothness` 0 to 1
    (default 0.7) trades deadzone width + glide speed against
    responsiveness.

    v1 used a plain damped velocity clamp. The review caught an
    accelerating boat lagging right out of frame, and the untracked lead-in
    parking the crop. Both are the controller's and the backward pass's
    reasons to exist. The face-based per-shot static plan stays the
    default.
  - **The backward pass is fixed-template NCC, not MIL** (h264 can't
    decode in reverse):
    - buffered forward decode in downscaled chunks (per-frame POS_MSEC
      seeks fed keyframe-quantized duplicates on VFR sources)
    - template auto-tightened to the box's most STRUCTURED sub-patch (user
      boxes carry slack water, and water texture decorrelates NCC within
      frames)
    - template kept FIXED (updating is how drift creeps in)
    - 0.45 NCC floor stops HONESTLY, with `covered_from` reporting the
      reach

    Reverse-MIL slid onto the subject's own wake (a boat retraces INTO its
    white streak) and reported success.
  - **box_distinctiveness guard**: (Sobel energy vs 8-neighbor median) ×
    (peak-subwindow concentration), measured on the init frame. Below 1.9
    every tracking surface WARNS the box may not contain a subject. Born
    from a live demo shipped with a box that contained only water. MIL has
    no confidence signal, so nothing else would ever complain.
  - `edit_video` `blur_faces {start?, end?, pixelate?}`: YuNet on EVERY
    frame, ±3-frame union smoothing. Detector dropouts stay covered:
    privacy over-covers, never under-covers.

    `blur_region {box, start?, end?, pixelate?}`: tracked blur patch
    (plates/logos/bystanders).

    Both render through a pipeline of cv2-decode, blur-ROI, rawvideo-stdin
    and ffmpeg x264, which muxes the original audio back.
  - ENGINE: TrackerMIL. The vendored VitTracker ONNX (opencv_zoo,
    Apache-2.0, models/) is DISABLED by default: opencv-python-headless
    5.0.0's new DNN graph engine mangles it. Measured 2026-07-22: zero
    boxes and a constant ~0.11 score on real and synthetic footage alike,
    and no engine opt-out env works. `VIDEO_TOOLS_TRACKER=vit` re-enables
    it after an opencv fix. MIL trade-offs documented: no scale adaptation
    (box keeps its initial size), no confidence score (weak lost-target
    detection).
  - Test lesson: flat or noise-dusted synthetic frames are PATHOLOGICAL
    for appearance trackers. MIL slid freely inside a flat white square
    and latched onto encode ghosts. The execution fixture is a multicolor
    blob over static SMPTE bars (real gradients, known ground-truth path).
    Tolerances are ±40 px on a 640-wide frame.
- **0.4.0 colour-management pack (2026-09, `color.py` + compiler)**, from
  a field report by an agent grading Sony HLG footage:
  - *The timeline is Rec.709 / limited range, enforced by the compiler.*

    - Every media chain declares its source colorimetry at the head with
      `setparams`. The values come from `color.input`, from the `convert`
      preset's implied source, or the player default for whatever the probe
      left unknown. The player default is 709 for HD and 601 up to 576
      lines.
    - The canvas-fit `scale` carries `out_color_matrix=bt709:out_range=tv`.
      This is the REAL conversion point, where 601-tagged and full-range
      sources convert and 709 sources pass bit-exact.
    - Fills generate in planar RGB and convert through the same explicit
      matrix.
    - Every base chain and the composited tail is pinned as 709, so the
      encoder writes a truthful VUI + `colr`.

    Measured before the pack:

    - An untagged HD source was decoded to RGB with the 601 matrix for
      every RGB filter (lut3d, curves, exposure, colortemperature, keying,
      the gbrp transitions).
    - Fills and PNG stills were converted with 601 and played back as 709
      (pure green stored Y145/U54/V34 instead of 172/42/27).
    - Outputs carried `unknown` or `bt470bg` tags.

    A `setparams` pin AFTER the conversion point only relabels. Hence the
    explicit scale options, and hence the head tag. Without it swscale
    reads an untagged HD source as 601 and "converts" it.
  - `color.input {matrix, primaries, transfer, range}` (aliases hlg / pq /
    rec709 / rec2020 / 601 / srgb / full / limited) for untagged or
    mistagged files.

    `color.convert: "hlg->rec709"`:

    - zscale to display-linear with HLG's 1000-nit OOTF (`npl=1000` puts
      HLG code 1.0 at linear 1.0)
    - +2.30 EV to anchor BT.2408 reference white (HLG 75 % = 203 nits) at
      SDR white
    - a Möbius knee (linear below 0.5, roll-off to the 4.926 signal peak),
      so highlights keep detail
    - then 709 primaries/transfer

    Measured: HLG 100/75/50/25 % maps to SDR Y 235/209/139/78. SDR 50 % grey
    survives SDR to HLG to SDR exactly. The conversion runs AFTER the canvas
    fit (+35 % on a 1080p encode, float pipeline). The common internet
    recipe (hable, npl=100) maps HLG codes onto themselves (the flat "HLG
    on an SDR display" look) and was rejected. `convert` + `match` on one
    clip is a validation error (the match would sample HLG pixels and apply
    in 709).
  - `lut` chains: a list, its entries a string or `{lut, strength}`.
    Per-entry strength is BAKED into the staged cube
    (`identity·(1−s) + lut·s`). A per-pixel frame blend is exactly this in
    LUT space, so it costs nothing at render time. Baking works for 1D and
    3D LUTs, with `DOMAIN_MIN/MAX` honoured.

    `color.strength` mixes the whole creative grade (hand grade + LUT
    chain) against the pre-grade frame. The chain: split, then grade, then
    `blend=all_mode=normal:all_opacity=S` (graded on the first input,
    measured). Both branches are pinned to one format (gbrap on overlays so
    alpha survives). Strength 1 emits the flat chain exactly as before.
    `input`, `convert` and `match` never enter the mix.
  - `clarity` (cas, contrast-adaptive, halo-free), then `sharpness`
    (unsharp 5x5, luma only, amount 1.5·s), after every LUT and after the
    canvas fit. Sharpening before a downscale is wasted. The finishing
    `sharpen` knob stays (released schema). Both on one clip warns.
  - `probe_media` reports the stream colour tags with an HLG/PQ/SDR
    verdict, whether the MP4/MOV container carries a `colr` box, and
    standard `tmcd` timecode. The box check is its own seek-based ISO-BMFF
    walk: ffprobe merges colr and the bitstream VUI into one report. Sony
    XAVC writes no colr.
  - `align_audio {ref, target, max_offset}`: GCC-PHAT on the 8 kHz mono
    waveforms (cross-power spectrum whitened to phase only), at most 20 min
    per file. Lags are clamped to the signal lengths, because beyond them
    the zero-padded circular correlation ALIASES. In the first test run a
    +54-frame peak read back as −1994.

    An onset-envelope correlation was the first design. It failed the live
    check on a drone clip's sea/wind ambience: −0.019 s at a flat peak for
    a true +1.750 s. There were no onsets to correlate. PHAT recovers that
    offset exactly at 85× its runner-up.

    Convention: target_time = ref_time + offset. Confidence = the PHAT
    peak's ratio to the best runner-up outside ±20 ms (strong ≥ 8, fair ≥
    3, weak below).
  - ffmpeg facts that shaped it (7.1.5 in the image, 7.0.2 locally, same
    behaviour):

    - The CLI's `-color_primaries` / `-color_trc` OUTPUT flags do not reach
      x264 (only `-colorspace` does). Frame props via `setparams` do, and
      the mov muxer then writes `colr`.
    - zscale fails on an unknown input transfer ("no path between
      colorspaces"). Explicit `tin/pin/min/rin` without explicit outputs
      fails the same way. Tag the frames instead.
- **0.4.1 (2026-09-05), `edit_video` colour contract + `pq->rec709`**:
  - *One-shot ops carry the timeline contract.* Every `edit_video` op that
    re-encodes video (trim, remove, crop, smart_reframe, resize, speed,
    speed_ramp, fps, concat, stabilize, match_color, motion_blur,
    burn_subtitles, to_gif, to_webp, blur_faces, blur_region) does this:

    - It declares the source's colorimetry at the chain head
      (`color.edit_contract`: the probe's known tags over the player
      defaults).
    - It converts matrix/range on the op's own scale. When the op has
      none, it converts through a no-size-change
      `scale=out_color_matrix=bt709:out_range=tv` (bit-exact on 709
      sources, measured).
    - It pins `format=yuv420p` + the Rec.709 tags, and so writes a
      truthful VUI + colr.

    Measured before the fix:

    - Untagged HD outputs stayed untagged.
    - A GIF of untagged HD decoded through 601 (pure green became
      18/255/6).
    - WebP was wrong for EVERY source. libwebp was fed raw YUV, and WebP
      decoders apply 601 by spec: 20/255/9 even for 709-tagged files. The
      op now converts to RGB in ffmpeg first.
    - The blur pipeline desaturated every TAGGED source. cv2 decodes with
      the file's tags, and ffmpeg re-encoded its RGB through the 601
      default: 709 green came back 144/54/34, untagged. The pipeline now
      returns through the matrix cv2 used and relabels. That matrix is the
      tags, or 601 for untagged files, which then keep the 601 round trip
      they always had.

    HDR (HLG/PQ) and BT.2020 sources PASS THROUGH with their tags and bit
    depth: a one-shot op never tone-maps (`concat` cannot and says so). The
    8-bit paths (the blur ops, interpolated `speed`) keep the tags on 8-bit
    output. `-c:v copy` ops are untouched. `match_color` samples through
    ffmpeg with the same head tag the chain carries (the composition
    pre-pass too). So the LUT is built in the RGB it is applied in.
  - *`pq->rec709`*: `zscale=t=linear:npl=203`, then 709 primaries, then
    `tonemap=mobius:param=0.5:peak=49.26`, then 709/limited. For PQ `npl`
    is a pure scale (measured: 100/203/1000/10000 nits map to
    0.49/1.00/4.95/49.26). So the BT.2408 anchor needs no exposure step,
    which matters because ffmpeg's `exposure` filter is capped at ±3 EV.

    The knee spans the whole PQ range, and nothing clips. 100/203/1000/10000
    nits map to SDR Y 179/211/231/235 (zscale encodes 709 with BT.1886).
    Everything under ~100 nits is linear, and SDR 50 % grey survives SDR to
    PQ to SDR exactly. HLG keeps `npl=1000` + 2.3 EV because its OOTF gamma
    depends on npl. `probe_media` names the preset that matches its
    verdict.
- **0.4.2 (2026-09-05): final renders land on 48 kHz audio.** Field report
  from the internal install's content agent: finals carried 96 kHz AAC. The
  footage was the first real Sony XAVC HLG clip, which the 0.4.x conversion
  passed.

  Cause, measured: `loudnorm` runs and EMITS 192 kHz even in linear mode.
  The AAC encoder then settles on 96 kHz, the highest rate it has.
  Previews skip loudnorm and were 48 kHz all along. Fix: `aresample=48000`
  after the loudnorm atom on the master bus and in the `edit_video`
  `loudness_normalize` op.

  Same report, not a bug: sharpening (`clarity` / `sharpness` / finishing
  `sharpen`) raises the bitrate at a constant CRF. High-frequency detail
  costs bits. So web deliverables should pass the `crf` override
  (23 balanced, 28 social) rather than lower the master's CRF 18. The skill
  says so now.
- **0.4.3 (2026-09-09): audio truth, the missing ops, caption fonts.**
  From the same agent's second shoot (camera track + mic recorder + music
  bed, a smart speaker answering on camera). Every number measured on
  ffmpeg 7.0.2 (the image's 7.1.5 carries the same options).
  - *Loudness normalization modes* (`audiofx.loudnorm_*`, renderer,
    `loudness_normalize`). ffmpeg's two-pass loudnorm with `linear=true`
    takes the linear branch ONLY when all three hold:

    - the static gain keeps the true peak under TP
    - the measured LRA is within the target LRA
    - none of the pass-1 values is a sentinel: TP 99, thresh −70, LRA 0,
      I 0. A constant-level source measures LRA 0.0 and always goes
      dynamic.

    Otherwise loudnorm silently runs its dynamic leveller, a program
    compressor. On the shoot's master the leveller lifted the quiet music
    intro by 11 dB and crushed LRA 6.3 to 3.5. The mix was blamed.

    The renderer now predicts the branch from pass 1 with that exact rule
    (verified against the pass-2 JSON `normalization_type` on three
    fixtures). It reports `mode → normalization_type`, the measured and
    target numbers and the gain. It WARNS on the auto fallback with the
    cause.

    `mode: "linear"` never enters loudnorm at pass 2. It applies a static
    `volume` to the target, then a true-peak ceiling at `true_peak` through
    a 4× oversampled `alimiter`, then goes back to 48 kHz. At 1× the
    limiter overshoots a −1.5 dBTP ceiling to −0.7 (inter-sample peaks).
    Integrated lands within 0.1 LU of the target, with LRA untouched.
    `mode: "dynamic"` is the leveller on request.

    Every final render (and the op) measures the DELIVERED file. AAC at
    192 kb/s adds up to ~1 dB of true-peak overshoot above the PCM ceiling
    (loudnorm's own −1.5 came out at −0.5). That is what the earlier field
    note "loudnorm ignores the true-peak target" was: the codec, not the
    filter.
  - *Latency compensation.* The sweetening stages do not flush their
    delay at EOF: output length unchanged, content late, tail lost. The
    lag per stage:

    - rnnoise by 480 samples (10 ms, it forces 48 kHz)
    - afftdn by 25 ms at any rate (1102/1200/2400 samples at
      44.1/48/96 kHz)
    - alimiter by 239 samples (its 5 ms lookahead, and `latency=1` cancels
      it exactly)

    The voice enhance chain therefore ran 14.98 ms late: the "+0.015 s
    every time" of the report.

    Chains that contain a delaying stage are built latency-neutral:

    - `aresample=48000` (the delays are known in 48 kHz samples)
    - `apad=pad_len=D` BEFORE the stages
    - `atrim=
      start_sample=D,asetpts=PTS-STARTPTS` after them

    The impulse is back at its sample, the length preserved, the tail
    kept. Every limiter carries `latency=1`. `enhance_audio` output is
    48 kHz from any source.
  - *Denoise mix.* `arnndn`'s `mix` blends filtered and input INSIDE the
    filter on aligned samples. Mix 0 / 0.5 / 1 all measured the same
    10 ms delay. The result: no comb filtering, one atom.

    `denoise: 0–1` is that wet share. The report's rule: a voice through
    room speakers reads as noise to the model (agent replies −4.8 / −6.4 dB,
    timbre lost), so blend.
  - *`level`* = `dynaudnorm=f=200:g=<window/0.2, odd>:p=0.9:m=<max_gain>
    :r=<target_rms>` in RMS-target mode, last in the chain. The defaults
    are the recipe validated by ear on the shoot's VO (4.2 s window,
    28 dB, −6 dBFS).

    The Gaussian window is preceded by a minimum filter, so the gain
    inside any window is pinned by its loudest frame. The filter levels
    changes SLOWER than the window.

    Measured on 3 s phrases at −14/−26, 12.1 became 12.1 dB with a 4.2 s
    window, 4.0 with 2.2 s and 2.7 with 1.4 s. A compressor (−18/3:1) took
    12.1 to 4.4 on the same, but at −15 dB of makeup pumping. `speechnorm`
    widened the real VO. Zero latency.
  - *Gain automation and fades.* `gain_keyframes` compiles to `volume=volume=
    'pow(10,(<piecewise dB>)/20)':eval=frame`, re-evaluated per audio frame
    (~21 ms). Splice edges belong to afade. Audio-track clips had
    `fade_in`/`fade_out` all along. Base clips gain `audio_fade_in`/
    `audio_fade_out`: their `fade_in`/`fade_out` were accepted and
    silently ignored, and validation says so now.
  - *`align_audio` bounds.* The lag clamp was `min(max_offset,
    len(ref)−1, len(target)−1)`, symmetric on the SHORTER file. So a
    12.97 s clip against a 49.55 s recording searched ±12.97 s while
    reporting "±60 s". The true +32.495 s was never a candidate. The best
    in-window lag (+2.709, PHAT peak 0.011, ratio 1.2) came back.

    Valid lags are −(len(ref)−1) … +(len(target)−1): inside that range
    the zero-padded circular correlation is exact, outside it aliases.
    With the fix the real pair yields +32.4946 s at 5.0× (the agent's
    excerpt method: 32.4950).

    The result now prints the searched range, the implied placement and
    overlap, the top-3 candidates, and a second opinion. The second
    opinion is the correlation of the two 50 ms log-RMS envelopes inside
    the overlap. On that pair it reads 0.66 to 0.77 at the true lag and
    0.19 to 0.30 at the wrong one.

    Any one of these marks the placement IMPLAUSIBLE and the grade weak:

    - overlap < 1 s
    - a peak at the search edge
    - agreement < 0.35 on a non-strong grade
  - *`rotate`* (`edit_video`): `transpose`/`hflip`/`vflip` under the
    colour contract. The source's display matrix is applied first by
    autorotate, and the output writes none (`-metadata:s:v:0 rotate=0`).
    The rig stores portrait footage turned with an identity matrix. The
    field pre-pass re-tagged HLG by hand, and the contract keeps the tags.
    `probe_media` prints a display-matrix tag and the size players show.
  - *Caption fonts.*

    - `font` (family)
    - `font_file`: staged under a safe name into a render `fontsdir`. The
      Style line carries the family the FILE declares, read with Pillow.
      libass matches fontsdir faces by that name, not by path.
    - `bold`/`italic` (the Style booleans, −1/0)

    The image adds `fonts-comfortaa` and `fonts-montserrat` (OFL).
    Validation warns on a family fontconfig does not know when no
    font_file is given: libass substitutes silently otherwise.

- **0.4.9 (2026-11-19): a render alone takes the whole memory budget.**
  From the Studio app's speed check: background renders of a 17 s edit took
  18.6 s (preview) and about 48 s (final) against 12.7 s and 28.4 s on
  0.4.6. Measured on the dev box with the same edit: 0.4.8 divided the
  budget by the runs allowed at once (`MAX_RENDERS`, 2). So 1,718 MB became
  859 MB for every render, alone or not. The edit then rendered in two
  windows instead of one pass: +47 % for the preview, +43 % for the final.
  The job runner costs nothing measurable: a job and a call took the same
  time.

  One gate now holds every heavy ffmpeg run (`fftools.heavy_run`): at most
  `MAX_RENDERS` at once, and the bytes they claim within the whole budget.
  A render window claims its own estimate. Any other heavy run claims an
  equal share, so two of them still go at once. A run alone always goes,
  and waiters are served in order. Windows are planned against the whole
  budget again. `VIDEO_TOOLS_RENDER_BUDGET_MB` is now the total the runs
  share.

  The memory warnings are gone: they were read only after the render
  succeeded, when nothing was left to do. A run the container's limit ends
  (SIGKILL) fails with a plain sentence that says what to do.

  `render_filmstrip` takes `start` and `end` (a timeline clip's in and out).
  A span writes a file name of its own.

  A preview has a keyframe every 2 s (`-g` = 2 × fps,
  `compiler.PREVIEW_KEYFRAME_S`). x264's own 250 frames gave one every 4 to
  8 s. Measured on the 134 s film's 540p preview, the file grew from
  3.49 MB to 4.22 MB (+21 %, the 17 s edit +14 %). The render time did not
  change. A jump decodes at most 58 frames instead of 238. With one thread
  and ffmpeg's own seek, the worst jump went from 163 ms to 68 ms and the
  median from 86 ms to 52 ms. One second costs +36 to +49 % for 8 ms more.
  The final keeps the encoder's spacing. It is the deliverable, every
  platform it goes to encodes it again, and bytes at CRF 18 matter more
  there.

- **0.5.0 (2026-11-23): every file opens beneath the mount.**
  `safe_fs.py` is the platform's own `proxy/services/infra/safe_fs.py`,
  byte for byte, and `shared.py` carries file-tools' helpers under their
  names. A file is opened beneath the mount with no link followed. Each
  call keeps the identity of every file beneath the mount it hands to
  ffmpeg, ffprobe or OpenCV: device, inode, size and change time. A later
  open of another file at that name is refused, a background job's
  included. So the file checked is the file used. The rule rests on the
  mount's filesystem giving each file its own inode and stamping each
  change, as a local disk does. A new link to the file and a change of its
  mode read as changed too, and so does a paired computer's file the
  platform writes anew. The call then says to run it again. ffmpeg, ffprobe and OpenCV get the open file (`/proc/self/fd/N`),
  with only the single-file formats the tool reads allowed, so a file
  changed after the probe cannot turn into a playlist. OpenCV reads through
  its FFmpeg reader alone: the build's other readers are off for the
  process. Any other argument that names the mount is refused. Outside the
  mount a file opens, and an output is written, only in the tool's own
  folders: its temp folders, the image's looks and models, and the
  slow-motion cache. The door reads the last argument alone as the output:
  every call site ends in its output or in `-`, and a new one must too. A
  value a child prints reaches a filter only as a number, or as a colour
  name of ffprobe's own list.

  An output lands on its name only when the run succeeds. The next render
  of that output sweeps a temp no writer can hold: one last written before
  the process started, or an hour ago. That rule reads a file's time
  against this machine's clock, so a clock set back, or a file server's
  clock behind it, makes a live temp look older. A cancel after a background
  render's file landed stops nothing and says so. Chromium loads a motion
  page from a routed origin, `https://motion.invalid/`, never opens a
  mount path, and a request its route does not see finds no network. A
  page is served at most 64 names and 256 MB, one request at a time. Two
  captures run at once, and a call waits for its turn only within its
  time: a clip's capture time, or a still's five minutes. The page's own
  promise of its fonts is waited for ten seconds at most. The blur's
  encode passes the gate and ends on a cancel, which reaches it between
  frames: one after the last frame lets its file land. Its wait at the
  gate has no limit of its own, as for every step of an edit. The proxy is
  asked on the async client with file-tools' wait for a file on a paired
  computer (150 s).

  A render gives each decoder and the x264 encoder a thread count: the
  container's cores and one, at most 16. Its memory estimate counts each
  open input's decoder frames per thread and x264's lookahead and
  references by that count. A source's frames count at the size their
  pixel format holds, so a 10-bit or 4:2:2 frame counts more than an 8-bit
  4:2:0 one. A transition counts both its inputs across its overlap. Four
  1080p clips with three dissolves peaked at 2,308 to 2,444 MB in the 0.4.6
  image (ffmpeg 7.1.5) with ffmpeg's own thread counts. With the render's
  count of 11 on ten cores they peaked at 2,319 MB. The estimate was
  2,239 MB and is now 3,201 MB.

  `render_motion_clip` stops capturing when the call's time is nearly
  spent, encodes the frames it has and says what the clip covers. Its
  encode, its wait at the gate included, ends while the call still has
  time for the answer, and a file that landed as that time ran out is
  answered. It sends
  progress when the call asks for it. Numbers in ffmpeg arguments keep six
  decimals, so a cut past 1,000 s keeps its frame. The slow-motion cache
  keys on the opened source's identity and the session's agent, and two
  builds of one clip write temps of their own. Its prune removes the temp
  of a process that ended during a build.

## Low-RAM windowed rendering

A composition renders as ONE ffmpeg filtergraph. ffmpeg's scheduler lets
decoded frames pile up in unbounded filtergraph queues at the fold/overlay
junctions. Peak RSS ≈ every decoded SOURCE frame of the render window at
once.

Measured on a real 70 s 1080p30 timeline (24 base clips + 10 overlays):
12.7 GB anon-rss OOM-killed a 15 GB host. The planner's decode estimate for
the same timeline is 14.1 GB.

Toy-scale experiments confirmed the shape:

- a 72 s 12-clip fold at 320x180 buffers exactly timeline × fps × frame_size
  (+190 MB over a 70 MB floor)
- the pile does NOT scale with the render canvas (it is source-side,
  pre-scale)
- a 6 s `time_range` slice used to cost the same as the full render, because
  nothing was pruned

The fix (renderer + compiler, `plan_segments`/`window_pruned`/
`estimate_window_bytes`/`bare_cut_points`):

- **Windowed final render.** When the whole-timeline decode estimate
  exceeds the memory budget, the timeline renders in windows. The budget
  defaults to 40% of physical RAM (cgroup-aware), and
  `VIDEO_TOOLS_RENDER_BUDGET_MB` overrides it. It is shared by the heavy
  runs that go at once (`fftools.heavy_run`), never split in advance. The windows concat
  losslessly (`-c copy`) and mux with the audio.

  Bare cuts are the preferred (free) split points. A span between adjacent
  bare cuts that alone busts the budget gains SYNTHETIC frame-grid points
  inside it (`MIN_WINDOW` 4 s floor, `MAX_SEGMENTS` backstop). So a single
  long continuous take, or two long clips joined by an xfade, window like
  anything else. Synthetic points never land within `SPLIT_GUARD` of a
  transition's overlap: the blend keeps real footage on both sides.
- **Window pruning.** Each window compiles from a sub-composition where
  out-of-window base clips become fills of IDENTICAL duration and
  out-of-window overlays are dropped. So far media is never opened or
  decoded. Timeline math, fold structure and transition offsets stay
  bit-identical: the substituted spans are trimmed away.

  A plain media clip that only PARTIALLY overlaps the window (the long-take
  case) is reduced to fill + sub-clip (+ fill) with the same total
  duration. The sub-clip's in-point advances onto the window, and its input
  opens with a decode-accurate `-ss` (`_seek`, trims rebased). So the
  source decodes only the window plus a small preroll, not from zero.

  Sides shorter than `MIN_TRIM` stay untrimmed. Stateful clips (speed ≠ 1,
  flow/blend interpolation, `_slomo`, `_stab`, `_match`, transform
  keyframes) always keep the whole-clip decode. A single live branch
  streams, so they are RAM-safe, just not decode-pruned. Every window
  recomputes the same timeline, so frame pts partition exactly at the
  edges: the concat is frame-exact, cuts stay cuts.
- **Frame-grid invariant (0.3.2, 2026-08).** "Identical duration" must hold
  in FRAMES, not just seconds, or window heads drift onto bare background.
  An unpinned `color=…:d=X` source emits **ceil(X·fps)** frames, while the
  media pin (`tpad`/`trim=end`/`fps`) emits **round-half-away(X·fps)**.
  That is one extra frame per fill whose `frac(d·fps) ∈ (0, 0.5)`. Speeded
  clips land there systematically: `span/speed` is rarely on the grid.

  Three guarantees now enforce the invariant:

  1. Fills and image clips carry a `trim=end_frame=N` pin with
     `N = composition.frames_for(d, fps)`. This is half-AWAY rounding,
     matching ffmpeg, never Python's banker's `round()`.
  2. `compute_timeline` quantizes every clip duration and xfade overlap to
     the frame grid, so entry boundaries and window splits are
     frame-aligned by construction.
  3. When a `time_range` render has overlays, one cloned `tpad` frame on
     the main branch before compositing keeps framesync's EOF-status pts
     past the final frame. Otherwise the fps re-stamp in the window tail
     flushes everything strictly below that status. Then the LAST timeline
     frame vanishes whenever an overlay is still active at stream end.

  Regression suite: `__tests__/test_frame_grid.py` (windowed vs single
  frame-for-frame identity on off-grid durations, an exact-half 3.75 s
  clip, a speeded clip, and a spanning alpha overlay).

  Known remaining nit: at one bare-cut window boundary a first frame can
  differ slightly in CONTENT (not count) from the single graph. The nit is
  pre-existing before 0.3.2, single-frame, tracked upstream.
- **Audio renders once.** One full-timeline audio-only pass (frames are
  ~KB) keeps amix, sidechain ducking and two-pass loudnorm semantics
  identical to a single-pass render.
- **`time_range` and `render_frames` prune too**, via the same
  `window_pruned` (with a pad covering transition/styling reach). A slice
  or QC frame no longer opens the whole timeline. The final-trim tail also
  re-pins `fps=`: trim clears the CFR metadata, and the sink silently
  resampled slices to 25 fps.
- **Limits.** Only a span whose MIN_WINDOW-sized pieces still bust the
  budget renders as one window (pathological resolutions), alone at the
  gate. If the container's memory cap kills it, the render fails with a
  sentence that says what to do. Project-level `grain`
  is temporally random, so its pattern reseeds at window edges (invisible
  in practice). Stateful clips keep whole-clip decode (see above): their
  decode CPU is unpruned, never their RAM.

## Decided at implementation

- Final-render delivery: a background job (`render_composition` with `background`,
  `render_status`, `cancel_render`), since an engine cuts a long silent call. A motion
  clip runs in the call: it reports progress when the call asks for it and keeps what
  it captured when the call's time runs out.
- Port 8933 (file-tools holds 8932 and 9981).
- Face detector: YuNet (ONNX, in the image). Reframe follows a tracked box when one is
  given, else a per-shot static crop.
- Schema: the file carries `version`, which is 1. `validate_composition` and every
  render refuse a composition of any other version as an error.
- `analyze_video` keeps its index beside the source (`<stem>.analysis.json`) when the
  session may write there.
