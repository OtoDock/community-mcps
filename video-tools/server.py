"""Video-Tools MCP Server — agent-driven video editing on FFmpeg.

Dual transport: SSE at /sse (Claude CLI) + streamable HTTP at /mcp (Codex CLI).
The agent edits a declarative composition file (<name>.vproj.json) in the
workspace; this server analyzes media, compiles the composition to an ffmpeg
filtergraph, and renders web-safe MP4.

Modules: shared.py (session binding, paths), fftools.py (ffmpeg layer),
composition.py (schema), compiler.py (filtergraph), renderer.py (orchestration),
captions.py, color.py, analysis.py, quickops.py, project.py (handlers).
"""

from contextlib import asynccontextmanager

import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from shared import MCP_PORT, logger, set_request_context

_CLIP_SCHEMA_DOC = (
    "CLIP fields — base (video) track: exactly one of src (media path) / "
    "image (still path) / fill ('#hex' solid); in/out = source seconds (media; "
    "omit out to use the file end); duration = seconds (image/color); "
    "speed 0.1–4 with interpolate: flow|blend|duplicate for real slow "
    "motion (speed < 1) — flow = motion-compensated synthesis (best; slow "
    "first render, cached after), blend = fast motion-smear, duplicate = "
    "default frame repeat (judders unless the source is high-fps; 60/120 "
    "fps sources retime natively, no synthesis needed); speed_ramp: "
    "{from, to, curve: linear|ease_in|ease_out} = SPEED RAMP (base media "
    "clips; replaces speed) — compiled as 6 constant-speed segments over "
    "the source span, slow segments use interpolate like plain slow "
    "motion; the classic move is {from: 1, to: 0.25, curve: ease_out} + "
    "interpolate: flow + motion_blur, clip muted with music over it; "
    "fit: cover "
    "(default, fills canvas) | contain (pads with "
    "background); transform: {scale, pos: [x,y] offset of the clip CENTER "
    "from the canvas center in project px}; color grade: {exposure ±3 EV, "
    "brightness ±0.5, contrast 0.3–2, saturation 0–2.5, gamma 0.4–2.5, "
    "temperature 2000–12000 K, curves {all/r/g/b: [[x,y]…] or "
    "{preset: name}}, lut: built-in look name or .cube path, "
    "match: SHOT COLOR-MATCHING (base clips) — {ref: 'path@seconds'} "
    "grades this clip to sit with that reference frame (drone vs camera, "
    "different takes), or bridge mode {ramp_from: 'clipA.mp4@11.9', "
    "ramp_to: 'clipB.mp4@0.1'} which ramps invisibly between a "
    "grade-matched start and end — THE fix for an AI transition bridge "
    "that doesn't color-match its neighbors (keep the cuts hard); "
    "strength 0–1}; "
    "transition_in: {type, duration} = the transition INTO this clip from "
    "the previous one; volume_db; mute; stabilize: true or {strength: "
    "low|medium|high, smoothing?, zoom?} — vidstab shake removal for "
    "handheld/windy-drone media clips (NOT gimbal/tripod shots; border "
    "compensation zooms in slightly; the analysis pass is cached, so "
    "preview/final/frames reuse it); motion_blur: true | 0–1 | {strength} "
    "— cinematic frame-stacking blur (strongest on high-fps or "
    "flow-interpolated footage); FILMIC FINISHING on base clips AND "
    "project-wide: vignette / grain / sharpen (each true, 0–1, or "
    "{strength|amount}), plus project.letterbox: '2.39' (cinemascope "
    "bars drawn over the frame; captions render on top of the bars).\n"
    "ANIMATION: transform.keyframes = [{t, scale?, pos?}, …] with t in "
    "clip-local seconds, linearly interpolated (replaces static scale/pos). "
    "Base clips animate scale ≥ 1 and pos — the Ken Burns move "
    "(e.g. [{t:0, scale:1, pos:[0,0]}, {t:5, scale:1.15, pos:[40,-20]}]). "
    "Overlays animate pos only.\n"
    "OVERLAY clips add: start (timeline seconds — REQUIRED), fade_in/"
    "fade_out (seconds, alpha fades), transform.opacity 0–1, transform."
    "rotate (degrees), effects: [{type: chromakey|colorkey|despill, color "
    "'#hex', similarity, blend}] (green-screen keying), mask: {image: "
    "grayscale path — white shows, black hides}. Overlays keep natural "
    "size (scale is relative) and honor PNG/WebM alpha — motion clips from "
    "render_motion_clip drop straight in as src.\n"
    "AUDIO clips: src, start (REQUIRED), in/out, gain_db, fade_in/fade_out, "
    "duck: true or {threshold, ratio, attack, release} — ducks this clip "
    "under everything that is not ducked: base-track audio plus other "
    "audio clips (a music bed dips under a voice-over clip even when the "
    "base video is silent).\n"
    "AUDIO SWEETENING (base + audio media clips): audio: {denoise: true | "
    "\"voice\" (speech NN model) | {strength: dB}, eq: {preset: voice|"
    "music|bright|warm|telephone} or {bands: [{f, gain_db, q?|width_hz?}]}, "
    "compress: true | {threshold_db, ratio, attack, release, makeup_db}, "
    "deess: true | {intensity}} — fixed order denoise→eq→compress→deess. "
    "Master bus: audio_master adds eq/compress/limiter (limiter: true | "
    "{ceiling_db} — the true-peak safety when loudnorm is off).\n"
    "Base-track clips are SEQUENTIAL: no gaps, no start field — order is "
    "position. Built-in looks: teal-orange, filmic, clean-punch, bw-classic, "
    "warm-golden, cool-matte, vivid, faded-retro."
)

TOOLS = [
    Tool(
        name="probe_media",
        description=(
            "Inspect a media file: container, duration, size, video codec/"
            "resolution/fps, audio codec/rate/channels. Cheap — use before "
            "planning any edit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Media file path"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="analyze_video",
        description=(
            "Detect shots (scene cuts) in a video and return a labeled contact "
            "sheet — one thumbnail per shot — plus a shot table with "
            "timestamps. This is your visual index of the footage: reference "
            "shots by their timestamps from here on. Results are cached next "
            "to the source (<stem>.analysis.json + <stem>.shots.png) for "
            "later sessions. Run once per source file before composing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Video file path"},
                "threshold": {
                    "type": "number",
                    "description": "Cut-detection sensitivity (lower = more cuts). Default 27.",
                    "default": 27,
                },
                "columns": {"type": "integer", "description": "Contact-sheet columns. Default 4.", "default": 4},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="analyze_audio",
        description=(
            "Analyze a music track / voice recording / video's audio: tempo "
            "(BPM), the full BEAT GRID (timestamps — snap cuts to these for "
            "music sync), energy curve, silence map, broadcast loudness "
            "(LUFS/peak/LRA), and a waveform image with beat ticks. The full "
            "beat grid + energy curve land in <stem>.analysis.json. "
            "Beat-syncing an edit = arithmetic against this grid, not "
            "listening."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Audio or video file path"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="sample_frames",
        description=(
            "Extract frames from a SOURCE media file at given timestamps and "
            "return them as one labeled grid image. Use to see specific "
            "moments before choosing in/out points. (For frames of a "
            "COMPOSITION use render_frames.)"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Video file path"},
                "timestamps": {
                    "type": "array", "items": {"type": "number"},
                    "description": "Seconds into the file (max 24)",
                },
                "columns": {"type": "integer", "default": 4},
            },
            "required": ["path", "timestamps"],
        },
    ),
    Tool(
        name="create_composition",
        description=(
            "Create a composition project file (<name>.vproj.json) — the "
            "declarative timeline this MCP renders. Pass project settings "
            "(width/height/fps/background) and optionally a full composition "
            "object. 1080x1920 for vertical short-form, 1920x1080 for "
            "landscape. The file lives in the workspace: iterate on it with "
            "edit_composition, check with validate_composition, render with "
            "render_composition. Edit ONLY through edit_composition — never "
            "write the .vproj.json with a file tool (on satellite installs a "
            "hand-written file can silently miss the render until the turn "
            "ends, so you render a stale timeline).\n\n" + _CLIP_SCHEMA_DOC
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Project file path, e.g. workspace/videos/launch.vproj.json"},
                "project": {
                    "type": "object",
                    "description": "{width, height, fps, background '#hex', color {global grade}}",
                },
                "composition": {
                    "type": "object",
                    "description": "Full composition object (advanced — overrides 'project')",
                },
                "create_new": {"type": "boolean", "description": "Overwrite if the file exists. Default false."},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="edit_composition",
        description=(
            "Apply edit operations to a composition file (sequential, "
            "continue-on-error, saved once). Operations:\n"
            "add_clip {track: 'video'|'overlay'|'audio'|index, clip: {…}, at?} "
            "(overlay/audio tracks auto-create) · update_clip {track, index, "
            "patch {…} — null deletes a key} · remove_clip {track, index} · "
            "move_clip {track, from, to} · set_transition {index, transition: "
            "xfade-name|'cut', duration} (transition INTO base clip index) · "
            "add_track {kind} · remove_track {track} · set_project {width/"
            "height/fps/background/color/vignette/grain/sharpen/letterbox} "
            "· set_captions {source: "
            ".transcript.json/.srt/.ass, preset: karaoke|word-pop|clean|"
            "minimal, position: lower_third|center|top, highlight_color, "
            "uppercase, font_size, max_words_per_cue, offset} · "
            "set_audio_master {gain_db, loudnorm: true|{target_lufs,…}}.\n"
            "Common transitions: fade, dissolve, circleopen, wipeleft, "
            "slideleft, smoothleft, zoomin, pixelize (full xfade set "
            "supported). WOW PRESETS (social-style, duration 0.1–0.6s): "
            "whip_pan, zoom_punch, flash_cut, glitch, spin, shake — these "
            "style the frames around a HARD CUT (no overlap, timeline "
            "unchanged) and hit hardest on a beat; glitch and shake are "
            "the proven social picks. flash_cut/zoom_punch dip to BLACK "
            "by default — {flash: 'white'} for the classic white pop. "
            "zoom_out / zoom_in (Kolder-style): the outgoing frame zooms "
            "through a MIRRORED tile (no black edges) with an eased "
            "slow-fast-slow ramp and building blur, the incoming zooms to "
            "rest — zoom_out pulls back, zoom_in punches forward. "
            "PREMIUM MOTION PRESETS (overlap like an xfade): whip_left / "
            "whip_right (a real camera whip: both frames WRAP around "
            "eased slow-fast-slow — no clip edge ever shows — and the "
            "swap hides under peak blur mid-whip) and luma_wipe (the "
            "next scene grows out of the brightest shapes of the outgoing "
            "frame — an organic mask-style reveal, no mask asset needed). "
            "Slow filmic joins: use fadeblack/fadewhite or dissolve at "
            "1.5–3s. Returns per-op results "
            "+ validation + the computed timeline.\n\n" + _CLIP_SCHEMA_DOC
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Composition file path"},
                "operations": {
                    "type": "array", "items": {"type": "object"},
                    "description": "Operations to apply in order",
                },
            },
            "required": ["path", "operations"],
        },
    ),
    Tool(
        name="validate_composition",
        description=(
            "Validate a composition (structure, files, transition feasibility, "
            "parameter ranges) and print the computed timeline — every base "
            "clip's start/end after transition overlaps. Renders run this "
            "automatically."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Composition file path"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="render_composition",
        description=(
            "Render a composition to MP4 (H.264/AAC, faststart — web-safe, "
            "plays inline via display_video with no re-transcode).\n"
            "mode=preview (default): fast, 540p-class, no loudness "
            "normalization — for iteration. mode=final: crf 18 preset slow + "
            "two-pass EBU R128 loudness normalization — the deliverable. "
            "`crf` overrides the mode's default quality (final 18 / preview "
            "27) — use ~28 to hit small web/site size budgets.\n"
            "range=[start,end] renders just that timeline slice (preview a "
            "transition without rendering the whole video).\n"
            "Workflow: preview → render_frames to inspect → adjust → final."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Composition file path"},
                "mode": {"type": "string", "enum": ["preview", "final"], "default": "preview"},
                "output_path": {"type": "string", "description": "Optional output path (default: sibling of the project file)"},
                "range": {
                    "type": "array", "items": {"type": "number"},
                    "description": "[start, end] seconds on the timeline — render only this slice",
                },
                "crf": {
                    "type": "integer", "minimum": 0, "maximum": 51,
                    "description": "x264 CRF override (final defaults 18, preview 27); ~28 for ≤20 MB site MP4s",
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="render_frames",
        description=(
            "Render single frames FROM THE COMPOSITION (all effects, "
            "transitions, captions, grades applied) at given timeline "
            "timestamps, returned as one labeled grid image. This is your "
            "visual QC loop: after each preview render, inspect cut points "
            "(±0.2s around transitions), caption moments, and framing. "
            "Adjust, re-render, re-check."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Composition file path"},
                "timestamps": {
                    "type": "array", "items": {"type": "number"},
                    "description": "Timeline seconds (max 16)",
                },
                "columns": {"type": "integer", "default": 3},
            },
            "required": ["path", "timestamps"],
        },
    ),
    Tool(
        name="render_motion_clip",
        description=(
            "Render agent-authored HTML/CSS (+JS) into a video clip, GIF, or "
            "animated WebP with a deterministic headless browser — the "
            "motion-graphics engine. Write normal web animation (CSS "
            "@keyframes, Web Animations API, or rAF-driven JS); every frame "
            "is stepped to exactly t = n/fps, so renders are reproducible.\n"
            "USE FOR: animated titles, lower thirds, callout boxes, logo "
            "stings (transparent=true → alpha WebM that drops into a "
            "composition as an OVERLAY clip src), full-frame title cards, "
            "and standalone social assets (gif/webp/mp4).\n"
            "Rules: design for the exact width×height given; the page has NO "
            "network access — inline all CSS/JS, reference workspace images "
            "with file:// absolute paths; system fonts (Inter, Roboto, Noto, "
            "color emoji, JetBrains Mono) are available by name; for "
            "transparent output leave the page background unset."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "html": {"type": "string", "description": "Inline HTML document (preferred)"},
                "html_path": {"type": "string", "description": "Or: path to an HTML file"},
                "width": {"type": "integer", "default": 1920},
                "height": {"type": "integer", "default": 1080},
                "fps": {"type": "number", "default": 30},
                "duration": {"type": "number", "description": "Clip length in seconds (0.2–120). REQUIRED."},
                "transparent": {"type": "boolean", "default": False,
                                "description": "Capture alpha (webm/gif/webp keep it; mp4 flattens over 'background')"},
                "background": {"type": "string", "default": "#000000",
                               "description": "Flatten color for mp4 when transparent=true"},
                "format": {"type": "string", "enum": ["mp4", "webm", "gif", "webp"],
                           "description": "Default: webm when transparent, else mp4"},
                "output_path": {"type": "string", "description": "Output file path. REQUIRED."},
            },
            "required": ["duration", "output_path"],
        },
    ),
    Tool(
        name="render_still",
        description=(
            "Render HTML/CSS to a single PNG/JPEG — the thumbnail, social "
            "card, and collage engine (same deterministic browser as "
            "render_motion_clip, one frame). Full CSS is available: layer "
            "workspace photos via file:// paths with object-fit, filters, "
            "mix-blend-mode, gradients, masks; big typography; CSS 3D "
            "transforms (perspective/rotateY) for 3D-look compositions.\n"
            "USE FOR: video thumbnails (1280x720, scale=2 for crisp text), "
            "social post cards, photo collages, quote cards, OG images. "
            "transparent=true → alpha PNG usable as a composition overlay "
            "or for further compositing.\n"
            "Same rules as motion clips: no network — inline CSS, reference "
            "workspace images by absolute file:// path; system fonts by "
            "name; 'at' freezes any animation at that second. The result is "
            "shown inline to the user automatically."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "html": {"type": "string", "description": "Inline HTML document (preferred)"},
                "html_path": {"type": "string", "description": "Or: path to an HTML file"},
                "width": {"type": "integer", "default": 1920},
                "height": {"type": "integer", "default": 1080},
                "scale": {"type": "number", "default": 1,
                          "description": "Device pixel ratio 1–3 — output is width×scale px (use 2 for thumbnails)"},
                "at": {"type": "number", "default": 0,
                       "description": "Freeze animations at this second (rarely needed)"},
                "transparent": {"type": "boolean", "default": False},
                "format": {"type": "string", "enum": ["png", "jpeg"], "default": "png"},
                "quality": {"type": "integer", "default": 92, "description": "JPEG quality"},
                "output_path": {"type": "string", "description": "Output file path. REQUIRED."},
            },
            "required": ["output_path"],
        },
    ),
    Tool(
        name="edit_video",
        description=(
            "One-shot edits on a single media file — no composition needed. "
            "Operations run in sequence (pipeline stops on first error):\n"
            "trim {start, end} · remove {start, end} (cut a segment OUT) · "
            "crop {aspect '9:16' centered, or x/y/width/height} · "
            "smart_reframe {aspect: '9:16'} (subject-aware: face detection "
            "picks a stable crop center per shot — the right way to turn "
            "landscape footage into vertical; plain crop centers blindly) · "
            "stabilize {strength: low|medium|high, smoothing?, zoom?} "
            "(vidstab two-pass shake removal for handheld/drone footage; "
            "slight zoom-in) · "
            "resize "
            "{width/height or short_side} · speed {factor 0.1–4, "
            "interpolate: flow|blend|duplicate — flow synthesizes real "
            "slow-motion frames when the source fps can't cover the "
            "slowdown} · speed_ramp {from, to, curve: "
            "linear|ease_in|ease_out — ramps the whole file's speed in 6 "
            "constant steps; slow segments duplicate frames here, so trim "
            "first and use a composition clip's speed_ramp + interpolate: "
            "flow for the cinematic version} · fps "
            "{fps} · concat {paths: […] appended after this file} · "
            "extract_audio {format: wav|mp3|aac|flac} · replace_audio "
            "{audio_path, mix_original_db?} · enhance_audio {preset: "
            "voice|music} (voice: NN denoise + clarity EQ + compression + "
            "de-ess + limiter — the one-shot interview/VO cleanup; "
            "stages can be overridden, e.g. denoise: false) · "
            "match_color {ref: 'path@seconds', target_time?, strength?} "
            "(grade this video to match a reference frame from another "
            "clip — shot matching) · "
            "motion_blur {strength: 0–1} (cinematic frame-stacking blur) · "
            "mute · volume {db} · "
            "loudness_normalize {target_lufs: -14} (two-pass R128) · "
            "burn_subtitles {subtitle_path: .transcript.json/.srt/.ass, "
            "preset: karaoke|word-pop|clean|minimal, position, "
            "highlight_color, uppercase} · to_gif {fps, width, start, end} · "
            "to_webp {fps, width, quality, start, end}.\n"
            "Writes <stem>_edited.<ext> next to the source unless "
            "output_path is given. The source file is never overwritten."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Input media file path"},
                "operations": {
                    "type": "array", "items": {"type": "object"},
                    "description": "Operations to apply in order",
                },
                "output_path": {"type": "string", "description": "Optional output path"},
            },
            "required": ["path", "operations"],
        },
    ),
]


# ===================================================================
# MCP SERVER SETUP
# ===================================================================

mcp_server = Server("video-tools")

sse = SseServerTransport("/messages/")
session_manager = StreamableHTTPSessionManager(app=mcp_server, stateless=True)


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handlers = {}
    # Lazy-load handlers to keep startup fast (librosa/cv2 imports are heavy).
    if name in ("probe_media", "analyze_video", "analyze_audio", "sample_frames"):
        import analysis
        handlers = {
            "probe_media": analysis.handle_probe_media,
            "analyze_video": analysis.handle_analyze_video,
            "analyze_audio": analysis.handle_analyze_audio,
            "sample_frames": analysis.handle_sample_frames,
        }
    elif name in ("create_composition", "edit_composition",
                  "validate_composition", "render_composition", "render_frames"):
        import project
        handlers = {
            "create_composition": project.handle_create_composition,
            "edit_composition": project.handle_edit_composition,
            "validate_composition": project.handle_validate_composition,
            "render_composition": project.handle_render_composition,
            "render_frames": project.handle_render_frames,
        }
    elif name == "edit_video":
        from quickops import handle_edit_video
        handlers = {"edit_video": handle_edit_video}
    elif name == "render_motion_clip":
        from motion import handle_render_motion_clip
        handlers = {"render_motion_clip": handle_render_motion_clip}
    elif name == "render_still":
        from motion import handle_render_still
        handlers = {"render_still": handle_render_still}

    handler = handlers.get(name)
    if not handler:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    try:
        result = await handler(arguments)
        if isinstance(result, list):
            return result
        return [TextContent(type="text", text=result)]
    except Exception as exc:
        logger.exception(f"Tool {name} failed")
        return [TextContent(type="text", text=f"Error: {exc}")]


# ===================================================================
# DUAL TRANSPORT ENDPOINTS — SSE (/sse) + Streamable HTTP (/mcp)
# ===================================================================


async def handle_sse(request):
    """SSE endpoint for Claude CLI and legacy MCP clients."""
    session_id = request.query_params.get("session_id", "")
    set_request_context(session_id, request.headers.get("authorization", ""))
    logger.info(f"SSE connection: session_id={session_id[:8] if session_id else '(none)'}...")
    async with sse.connect_sse(
        request.scope, request.receive, request._send,
    ) as streams:
        await mcp_server.run(
            streams[0], streams[1],
            mcp_server.create_initialization_options(),
        )


async def mcp_asgi_app(scope, receive, send):
    """Streamable HTTP ASGI app for Codex CLI and modern MCP clients."""
    if scope["type"] == "http":
        from starlette.requests import Request
        request = Request(scope, receive, send)
        session_id = request.query_params.get("session_id", "")
        set_request_context(session_id, request.headers.get("authorization", ""))
        logger.info(f"MCP request: session_id={session_id[:8] if session_id else '(none)'}...")
    await session_manager.handle_request(scope, receive, send)


async def handle_health(request):
    return JSONResponse({"status": "ok"})


@asynccontextmanager
async def lifespan(app):
    async with session_manager.run():
        yield


starlette_app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
        Mount("/mcp", app=mcp_asgi_app),
        Route("/health", endpoint=handle_health),
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    logger.info(f"Video-Tools MCP starting on port {MCP_PORT}")
    uvicorn.run(starlette_app, host="0.0.0.0", port=MCP_PORT, log_level="info")
