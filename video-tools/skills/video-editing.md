# Video Editing (video-tools)

You edit video by writing a **composition** — a declarative timeline file
(`<name>.vproj.json`) that video-tools compiles and renders with FFmpeg. You
never watch video: you read **structured analysis** (shots, beat grids,
transcripts, loudness) and you look at **rendered frames**. The human does
the final full-speed watch.

## The workflow (always in this order)

1. **Analyze every source once.**
   - `analyze_video` on each footage file → shot list + contact sheet. This
     is your visual index; refer to shots by timestamp from here on.
   - `analyze_audio` on the music track → BPM + the full **beat grid**
     (timestamps in `<stem>.analysis.json`), energy curve, loudness.
   - `probe_media` for quick facts; `sample_frames` to see exact moments
     before choosing in/out points.
   - Analysis is cached in sidecars — re-runs are free across sessions.
2. **Plan on paper, in timestamps.** Pick shots, decide the cut rhythm, and
   for music-driven edits snap EVERY cut to a beat: cut points = beat-grid
   arithmetic, not feel. Chorus/drop sections (energy curve) get the fastest
   cutting and the strongest shots.
3. **Compose.** `create_composition` → `edit_composition` with clips,
   transitions, overlays, music, captions. Edit the project ONLY through
   `edit_composition` — NEVER write the `.vproj.json` with a file tool: on
   satellite installs a hand-written file can silently miss the render until
   the turn ends, and you render a stale timeline without knowing it.
4. **Preview render.** `render_composition` mode=preview (fast, small).
   Use `range=[a,b]` to iterate on one section without re-rendering all.
5. **Look at it.** `render_frames` at: ±0.2s around every transition, each
   caption's midpoint, overlay in/out moments, and 2–3 random spots. Check
   framing, caption legibility, overlay position, grade consistency. Fix →
   re-preview → re-check. Do not skip this loop.
6. **Final render.** mode=final (high quality + two-pass loudness
   normalization). Show the user with `display_video` (it plays inline —
   the output is web-safe, no re-transcode).

## Editing taste (defaults that read as professional)

- **Cuts beat transitions.** Most joins should be hard cuts. Use a
  transition to mark a real change (scene, chapter, mood) — typically
  `fade`/`dissolve` 0.3–0.6s. Showy wipes (`circleopen`, `slideleft`,
  `pixelize`) are for high-energy short-form, sparingly.
- **Wow presets** (`whip_pan`, `zoom_punch`, `flash_cut`, `glitch`,
  `spin`, `shake`) style the frames around a HARD CUT — the cut stays a
  cut and the timeline doesn't shorten. Rules: land them ON a beat
  (ideally a drop), 0.2–0.4s duration, at most one every few cuts — a
  reel where every cut whips reads amateur. **First picks for social:
  `glitch` and `shake`** (verified on real footage — the RGB
  glitch pop and the impact drop). `flash_cut`/`zoom_punch` dip to BLACK
  by default; add `"flash": "white"` on the transition for the classic
  white pop. Match energy: whip presets/spin for camera-motion joins,
  zoom_punch/shake for impact moments, flash_cut for reveals, glitch for
  tech/gaming content.
- **Premium motion presets**: `whip_left`/`whip_right` (overlap like an
  xfade) are a true camera whip — the two shots form one continuous
  STRIP: the frame rushes left (or right) and the next shot slides in
  attached on that side, seam feathered into a blur band (never a hard
  line), speed and directional blur both riding a slow-fast-slow ramp.
  Strongest when the outgoing shot already pans that way.
  `zoom_out`/`zoom_in` (hard-cut core) are the Kolder zoom — ONE
  continuous motion across the cut: `zoom_in` accelerates INTO the
  outgoing frame, then the next shot arrives pulled-out through its
  MIRRORED tile (revealed edges are reflections, never black) and keeps
  pushing in to rest; `zoom_out` is the exact reverse (pull back through
  mirrors, arrive magnified, settle out). Mirror-tiled clips cost ~4×
  render pixels — keep those clips short.
  **Zoom presets need DIFFERENT scenes on the two sides.** Between two
  shots of the same scene (same subject at two distances) the arriving
  magnified frame looks like the shot you just left — the viewer reads a
  jump cut backward in time. For a same-scene jump (close → wide of the
  same subject) hide the cut with a whip (`whip_pan`/`whip_left`) or
  `glitch` — hiding jump cuts is what whips are for. `luma_wipe` (overlap) reveals
  the next scene through the brightest shapes of the outgoing frame — an
  organic, mask-like reveal with no mask asset; end the outgoing shot on
  a clear bright subject (sky, wake, window) for the "grows out of the
  object" effect.
- **Every transition takes a `duration`** — pacing is the knob: social =
  fast (0.2–0.4s presets), filmic = slow (`dissolve` or `fadeblack`/
  `fadewhite` at 1.5–3s reads as cinema; `fade` IS the standard
  crossfade). A 2s `whip_left` becomes a dreamy drift — legal, but know
  why you're doing it.
- **Short-form pacing:** 1080x1920, clips 1.5–3s between cuts, first 2s must
  hook (start mid-action, caption on screen from t=0).
- **Landscape footage → vertical:** run `edit_video` with
  `smart_reframe {aspect: "9:16"}` FIRST (face detection picks a stable
  per-shot crop that follows the subject), then compose from the reframed
  file. A plain center crop beheads off-center subjects.
- **When the subject has no face** (boats, cars, animals, products),
  smart_reframe falls back to a center crop — its result note says which
  shots were subject-tracked. Do NOT accept the fallback blind: sample
  frames of the source, see where the subject actually sits, and bias the
  crop yourself — `crop {x, y}` in edit_video, or `fit: "cover"` +
  `transform.pos: [dx, dy]` on the clip (positive x shifts the picture
  right, so a subject on the LEFT of frame needs positive dx to come to
  center). Re-check with render_frames after. The same rule applies to any
  aspect mismatch (vertical source on a landscape canvas too).
- **Music first:** place the track, read its beat grid, then cut picture to
  it. Duck music under speech with `duck: true` on the music clip.
- **Levels:** speech-led videos target the default −14 LUFS master; music
  beds sit −6 to −12 dB under (set `gain_db` on the music clip). Final
  renders normalize automatically — set `audio_master.loudnorm.target_lufs`
  to −16 for calmer platforms (YouTube) and keep −14 for short-form.

## Audio sweetening (make camera audio sound produced)

- Per-clip chain on base/audio media clips, fixed order
  denoise → eq → compress → deess:
  `audio: {"denoise": "voice", "eq": {"preset": "voice"},
  "compress": true, "deess": true}` is the interview/VO recipe.
- `denoise: "voice"` (neural, speech only — strongest on voice, eats
  music) vs `true` (broadband spectral, safe everywhere; tune
  `{strength, floor_db}` for hiss level). Never denoise a clean studio
  track — it can only cost air.
- EQ presets: voice (rumble cut + presence + air), music (gentle smile),
  bright, warm, telephone (stylistic band-limit). Or explicit
  `bands: [{f, gain_db, q}]` — same restraint as color: ±2–3 dB moves.
- `compress: true` is voice-tuned (−18 dB, 3:1). Music beds want gentler:
  `{threshold_db: -14, ratio: 2}`.
- Master bus: `audio_master.eq/compress` for glue, and
  `limiter: {ceiling_db: -1}` as the true-peak safety whenever you set
  `loudnorm: false` (loudnorm already limits when on).
- One-shot cleanup without a composition: `edit_video` op
  `enhance_audio {preset: voice|music}` — denoise + EQ + compression +
  de-ess + limiter in one pass (override stages, e.g. `denoise: false`).

## Stabilization (handheld & drone)

- `stabilize: true` on a media clip (or the `edit_video` op
  `stabilize {strength}`) removes camera shake via two-pass motion
  analysis. Presets `strength: low|medium|high` (default medium) — higher
  smooths the camera path harder but zooms in more (border compensation).
  Fine-tune with `smoothing` (frames of path averaging) and `zoom`
  (extra %).
- Stabilize handheld walking shots, windy drone footage, vehicle mounts.
  Do NOT stabilize gimbal/tripod/static shots — nothing to fix, and the
  zoom-in costs composition.
- The motion analysis is cached next to the source: the first render pays
  for it once; previews, frame checks, and the final all reuse it.
- QC with render_frames near the clip's edges: very strong shake can leave
  border artifacts — raise `zoom` a touch or drop a strength level.

## Real slow motion

- `speed` goes down to 0.1. Below ~0.5 on ordinary footage, ask for frame
  synthesis: `"speed": 0.25, "interpolate": "flow"` (motion-compensated —
  the cinematic look) or `"blend"` (fast preview-grade, motion-smear).
  Without it frames duplicate and the motion judders — the validator
  warns when the source fps can't cover a slowdown.
- **High-fps sources are the real answer**: 60 fps covers 0.5×, 120 fps
  covers 0.25× natively — perfect slow motion with zero synthesis
  (video-tools retimes native frames automatically and tells you).
  Shoot 60/120 whenever slow motion is planned.
- `flow` pre-renders the slowed span once (minutes for long spans) and
  caches it — previews, frame checks, and the final reuse it. Tell the
  user before the first flow render of a long clip.
- Extreme slow-mo audio is a droning smear: `mute: true` the clip and lay
  music/SFX instead. Combined with `stabilize`, stabilization is applied
  before synthesis (the right order) automatically.
- **Motion blur** (`motion_blur: true | 0–1` on a clip, or the
  `edit_video` op): temporal frame stacking for the cinematic
  shutter-drag look. It needs frames that differ — strongest on high-fps
  sources and flow-interpolated slow motion; pair `interpolate: "flow"`
  + `motion_blur` for buttery action slow-mo.

## Speed ramps (the Kolder move)

- `speed_ramp: {"from": 1.0, "to": 0.25, "curve": "ease_out"}` on a base
  clip: realtime into the action, easing down into slow motion right at
  the peak moment. Reverse (`from` slow, `to` fast) accelerates OUT of a
  moment. It replaces `speed` — never set both.
- The full recipe: `speed_ramp` + `interpolate: "flow"` + `motion_blur:
  0.4` + `mute: true` with music over it. Ramped audio steps through
  tempo changes segment by segment — always mute below ~0.5×.
- How it works (and what that means for you): the ramp compiles to 6
  constant-speed segments over the source span. Curves are sampled in
  SOURCE time, so the slow end occupies most of the output. A transition
  on a ramped clip only has the edge segment (~1/6 of the source span) to
  play in — the validator checks the room, but keep transitions into
  ramps short (0.2–0.4 s) regardless.
- Not combinable with `transform.keyframes` (Ken Burns) or `color.match`
  on the same clip — segmentation would restart both. Match or grade the
  file first via `edit_video`, then ramp the result.
- `edit_video` also has a one-shot `speed_ramp {from, to, curve}` for the
  whole file — quick previews of a ramp feel; it duplicates frames on the
  slow end, so the composition path is the deliverable one.

## Color (restraint wins)

- Prefer a built-in look over hand grading: `color: {"lut": "clean-punch"}`
  per clip or on `project.color` for the whole video. Looks: teal-orange,
  filmic, clean-punch, bw-classic, warm-golden, cool-matte, vivid,
  faded-retro. User `.cube` LUTs work by path.
- Hand adjustments: one or two moves, small values (saturation 1.05–1.15,
  contrast 1.03–1.10, exposure ±0.3 EV). Same restraint rules as photo
  editing: subtle moves compound; maxed sliders read amateur.
- Grade consistency across cuts matters more than any single clip looking
  great — same look on every clip of a scene.
- **Filmic finishing** (clip or project-wide): `vignette`, `grain`,
  `sharpen` — each `true`, a 0–1 strength, or `{strength|amount}`. Grain
  0.2–0.4 + a subtle vignette sells "shot on film"; project-wide beats
  per-clip for consistency. `project.letterbox: "2.39"` draws cinemascope
  bars over the frame (captions sit on the bars) — keep subjects inside
  the band, and pair it with `filmic`/grain for the cinematic package.
- **Shot matching (`color.match`)**: when two sources don't sit together
  (drone vs camera, different takes, an AI-generated bridge), grade the
  odd one to a reference frame from the good one:
  `color: {"match": {"ref": "workspace/camera.mp4@11.8"}}` — then put any
  creative look ON TOP (match normalizes first). `strength: 0.7` for a
  partial pull.
- **AI-bridge joins**: keep the cuts HARD and ramp-match the bridge to
  both neighbors: `color: {"match": {"ramp_from": "clipA.mp4@11.9",
  "ramp_to": "clipB.mp4@0.1"}}` — the bridge starts graded like A's last
  frame and ends graded like B's first, dissolving between the two grades
  of the same footage. Invisible; never hide a color mismatch with a
  crossfade. Sample refs at the junction frames (similar content is what
  makes the match exact).

## Captions (short-form's most important element)

- Pipeline: transcribe the SOURCE with the transcribe MCP (word-level
  timestamps land in `<stem>.transcript.json`) → set
  `captions: {"source": "<stem>.transcript.json", "preset": …}`.
  Caption times must match the FINAL timeline: if the speech clip starts at
  0 on your timeline and you trimmed the source from 12.4s, transcribe the
  trimmed/rendered cut instead — or pass `offset` to shift.
- Presets: `karaoke` (word fill — the TikTok default), `word-pop` (one big
  word at a time — hook sections), `clean` (broadcast), `minimal` (boxed,
  unobtrusive). `highlight_color` takes a brand hex; `uppercase: true` for
  the aggressive style.
- Always `render_frames` on 2–3 caption moments: check size, margin
  collisions, and that the highlight lands on the spoken word.

## Composition mental model

- ONE base `video` track: clips are **sequential** — order is position, no
  gaps. `transition_in` on a clip overlaps it into the previous one (the
  timeline shortens by the transition duration; validate_composition prints
  the computed timeline).
- `overlay` tracks: explicit `start`, natural size, `pos` = center offset
  in project pixels, PNG/WebM alpha honored, `fade_in`/`fade_out` are alpha
  fades. Use for logos, screenshots, lower-third images, motion clips.
- `audio` tracks: explicit `start`, `gain_db`, fades. `duck` sidechains a
  clip under everything not ducked — base-track speech AND other audio
  clips, so a music bed dips under a separate voice-over clip too.
- Stills (`image`) and solid fills (`fill: "#0a0a12"`) are first-class base
  clips — title cards, background beds.

## Movement (Ken Burns) and keying

- **Every still image on the base track should move.** A static photo reads
  as a slideshow; a slow push-in reads as a film:
  `transform.keyframes: [{t: 0, scale: 1.0, pos: [0,0]},
  {t: <clip end>, scale: 1.08, pos: [20, -10]}]`. Keep it subtle
  (scale 1.05–1.3 over the whole clip); alternate push-in / pull-back /
  lateral drift between consecutive stills. Works on video clips too
  (slow zoom toward the subject for emphasis).
- Overlays animate **position** the same way (fly-ins:
  keyframes from off-canvas pos to the resting pos) — though for anything
  beyond a straight slide, build the animation in `render_motion_clip`
  instead (easing curves live there).
- Green-screen: `effects: [{type: chromakey, color: "#00FF00",
  similarity: 0.1}, {type: despill}]` on an overlay clip. Shape cutouts:
  `mask: {image: gray.png}` (white shows, black hides).

## Motion graphics (`render_motion_clip`)

The title/lower-third/callout engine. Write plain HTML/CSS animation, get a
deterministic clip back:

- **Overlay elements**: `transparent: true` → alpha WebM → use directly as
  an overlay clip's `src`. Design at the project resolution and leave the
  page background unset. Animated titles, name straps, feature callouts,
  logo stings, animated arrows/highlights.
- **Full-frame cards**: opaque render (mp4) → base-track clip. Intros,
  section breaks, end cards.
- **Standalone assets**: gif/webp for posts and READMEs.
- Craft rules: real easing (`cubic-bezier`, not linear), animate transform/
  opacity, 2–4 elements max per clip, match the video's color palette and
  fonts (Inter/Roboto/Noto/emoji available), end states via
  `animation-fill-mode: forwards`. No network — inline everything;
  workspace images via `file://` absolute paths.
- **Stills** (`render_still`): the same engine, one frame — thumbnails
  (1280×720 with `scale: 2` for crisp text), social cards, photo collages,
  quote cards. Layer real photos with `background-image`/`object-fit`, CSS
  `filter`/`mix-blend-mode`, gradient scrims under text, and CSS 3D
  transforms (`perspective` + `rotateY`) for the floating-screenshot look.
  Division of labor: file-tools edits PHOTOGRAPHS (tone/color of real
  pixels); image-gen generates imagery; render_still COMPOSES designed
  layouts from both.

## Quick edits without a composition

`edit_video` for single-file jobs: trim, remove a segment, crop to 9:16,
resize, speed, concat, extract/replace audio, two-pass loudness normalize,
burn subtitles, GIF/WebP export. It never overwrites the source.

## Performance expectations

- preview ≈ well under a minute for short videos; final is several times
  slower (crf 18, preset slow, loudness measurement pass) — say so before
  long final renders.
- Renders are CPU-bound and serialized (two at a time) — batch your
  iteration into preview+frames rounds rather than many tiny final renders.
