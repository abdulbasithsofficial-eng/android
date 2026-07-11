#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║         BasiXa AI Video Agent — Renderer             ║
║         Kokoro TTS + Pollinations AI + FFmpeg        ║
╚══════════════════════════════════════════════════════╝

HOW TO USE:
  1. Pehle agent.html browser mein kholo
  2. Topic dalo, script generate karo
  3. "Download Python" click karo — scenes.json bhi download hoga
  4. scenes.json aur ye file same folder mein rakho
  5. python render_video.py
"""

import os, sys, json, time, requests, soundfile as sf
from pathlib import Path
from PIL import Image

# ─── Check dependencies ───────────────────────────────
def check_deps():
    missing = []
    try: import kokoro_onnx
    except: missing.append("kokoro-onnx")
    try: import soundfile
    except: missing.append("soundfile")
    try: from moviepy.editor import ImageClip
    except: missing.append("moviepy")
    try: from PIL import Image
    except: missing.append("pillow")
    try: import requests
    except: missing.append("requests")
    if missing:
        print(f"\n❌ Missing packages: {', '.join(missing)}")
        print(f"   Run: pip install {' '.join(missing)}\n")
        sys.exit(1)

check_deps()

from kokoro_onnx import Kokoro
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips

# ─── Config ───────────────────────────────────────────
KOKORO_MODEL = "kokoro-v0_19.onnx"
KOKORO_VOICES = "voices.bin"
SCENES_FILE = "scenes.json"
OUTPUT_DIR = "output"
SCENES_DIR = "scenes"
FPS = 24

# ─── Load scenes.json ─────────────────────────────────
def load_scenes():
    if not Path(SCENES_FILE).exists():
        print(f"\n❌ scenes.json nahi mila!")
        print(f"   Agent se script generate karo phir 'Download Python' click karo.")
        print(f"   scenes.json aur render_video.py same folder mein hone chahiye.\n")
        sys.exit(1)
    with open(SCENES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data

# ─── Download image from Pollinations ─────────────────
def download_image(prompt, filepath, retries=3):
    url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt + ', cinematic, 4k, high quality, detailed')}"
    for attempt in range(retries):
        try:
            print(f"    🖼  Downloading image (attempt {attempt+1})...")
            r = requests.get(url, timeout=90)
            r.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(r.content)
            # Resize to 1920x1080
            img = Image.open(filepath).convert("RGB")
            img = img.resize((1920, 1080), Image.LANCZOS)
            img.save(filepath, "JPEG", quality=95)
            return True
        except Exception as e:
            print(f"    ⚠️  Attempt {attempt+1} failed: {e}")
            time.sleep(3)
    return False

# ─── Ken Burns zoom effect ────────────────────────────
def make_zoom_clip(img_path, duration, direction="in"):
    clip = ImageClip(img_path).set_duration(duration)
    if direction == "in":
        zoom = lambda t: 1 + 0.04 * (t / duration)
    else:
        zoom = lambda t: 1.04 - 0.04 * (t / duration)
    return clip.resize(zoom)

# ─── Main ─────────────────────────────────────────────
def main():
    print("\n" + "═"*55)
    print("  🎬 BasiXa AI Video Agent — Starting Render")
    print("═"*55 + "\n")

    # Check Kokoro model files
    if not Path(KOKORO_MODEL).exists() or not Path(KOKORO_VOICES).exists():
        print("❌ Kokoro model files nahi mile!")
        print(f"   Download karo: https://github.com/thewh1teagle/kokoro-onnx/releases")
        print(f"   Chahiye: {KOKORO_MODEL} aur {KOKORO_VOICES}")
        print(f"   Dono files is script ke saath same folder mein rakho.\n")
        sys.exit(1)

    # Load scene data
    data = load_scenes()
    scenes = data.get("scenes", [])
    title = data.get("title", "AI_Video")
    voice = data.get("voice", "af_heart")
    lang  = data.get("lang", "en-us")

    print(f"📋 Title  : {title}")
    print(f"🎙  Voice  : {voice}")
    print(f"🎬 Scenes : {len(scenes)}")
    print(f"📁 Output : {OUTPUT_DIR}/\n")

    os.makedirs(SCENES_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load Kokoro
    print("🔊 Loading Kokoro TTS model...")
    kokoro = Kokoro(KOKORO_MODEL, KOKORO_VOICES)
    print("✅ Kokoro ready!\n")

    clips = []
    total = len(scenes)
    start_time = time.time()

    for i, scene in enumerate(scenes):
        n = str(i + 1).zfill(3)
        audio_path = f"{SCENES_DIR}/audio_{n}.wav"
        image_path = f"{SCENES_DIR}/image_{n}.jpg"

        print(f"[{i+1}/{total}] {scene['title']}")
        print(f"{'─'*50}")

        # Generate TTS
        if not Path(audio_path).exists():
            print(f"    🎙  Generating voice...")
            try:
                samples, sr = kokoro.create(
                    scene["narration"],
                    voice=voice,
                    speed=1.0,
                    lang=lang
                )
                sf.write(audio_path, samples, sr)
                duration = len(samples) / sr
                print(f"    ✅ Audio: {duration:.1f}s")
            except Exception as e:
                print(f"    ❌ TTS failed: {e}")
                continue
        else:
            print(f"    ⏭  Audio cached")

        # Download image
        if not Path(image_path).exists():
            ok = download_image(scene["imagePrompt"], image_path)
            if not ok:
                print(f"    ⚠️  Image failed, using black frame")
                img = Image.new("RGB", (1920, 1080), (10, 10, 20))
                img.save(image_path)
            else:
                print(f"    ✅ Image saved")
        else:
            print(f"    ⏭  Image cached")

        # Make clip
        try:
            audio = AudioFileClip(audio_path)
            direction = "in" if i % 2 == 0 else "out"
            video = make_zoom_clip(image_path, audio.duration, direction)
            video = video.set_audio(audio)
            clips.append(video)
            elapsed = time.time() - start_time
            remaining = (elapsed / (i+1)) * (total - i - 1)
            print(f"    🎬 Clip ready | ETA: {int(remaining//60)}m {int(remaining%60)}s\n")
        except Exception as e:
            print(f"    ❌ Clip error: {e}\n")

    if not clips:
        print("❌ Koi clip nahi bani. Check karo errors upar.")
        sys.exit(1)

    # Render final video
    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title)
    output_path = f"{OUTPUT_DIR}/{safe_title}.mp4"

    print("═"*55)
    print(f"🎬 Rendering final video... ({len(clips)} scenes)")
    print("═"*55)

    # Apply transitions (crossfades) between clips in MoviePy
    clips_with_transitions = [clips[0]]
    trans_dur = 0.4
    for clip in clips[1:]:
        clips_with_transitions.append(clip.crossfadein(trans_dur))
    final = concatenate_videoclips(clips_with_transitions, padding=-trans_dur, method="compose")
    final.write_videofile(
        output_path,
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        bitrate="5000k",
        threads=4,
        preset="fast",
        logger=None
    )

    total_time = time.time() - start_time
    print(f"\n{'═'*55}")
    print(f"  ✅ VIDEO READY!")
    print(f"  📁 File    : {output_path}")
    print(f"  ⏱  Duration: {final.duration/60:.1f} minutes")
    print(f"  🎬 Scenes  : {len(clips)}")
    print(f"  ⏰ Took    : {int(total_time//60)}m {int(total_time%60)}s")
    print(f"{'═'*55}\n")

if __name__ == "__main__":
    main()
