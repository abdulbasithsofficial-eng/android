#!/usr/bin/env python3
import json
import os
import sys
import re
import hashlib
import shutil
import subprocess
import threading
import time
import uuid
import wave
import asyncio
import random
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory

try:
    import imageio_ffmpeg
except Exception:
    imageio_ffmpeg = None


import sys
import os

if getattr(sys, 'frozen', False):
    # PyInstaller temporary extraction folder
    EXTRACT_ROOT = Path(sys._MEIPASS)
    # The parent directory of the .exe file
    EXE_DIR = Path(sys.executable).resolve().parent
    # Dev override: if running from 'dist' and root project 'runs' folder exists, use project root
    if EXE_DIR.name.lower() == 'dist' and (EXE_DIR.parent / 'runs').exists():
        EXE_DIR = EXE_DIR.parent
else:
    EXTRACT_ROOT = Path(__file__).resolve().parent
    EXE_DIR = Path(__file__).resolve().parent

ROOT = EXTRACT_ROOT
RUNS = EXE_DIR / "runs"
OUTPUT = EXE_DIR / "output"
ASSETS = EXE_DIR / "assets"

for folder in (RUNS, OUTPUT, ASSETS):
    folder.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=None)

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        response = app.make_default_options_response()
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

@app.after_request
def add_header(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return r

jobs = {}
pexels_lock = threading.Lock()
SUBPROCESS_FLAGS = 0x08000000 if sys.platform == "win32" else 0
DEFAULT_PEXELS_API_KEY = "Ij1bLuf8B89auuryUCvE8vionSThypnLtfgkf4pvZ1NYlUBiwkszXYx1"

GLOBAL_USED_MEDIA_FILE = RUNS / "global_used_media.json"

def load_global_used_media():
    if GLOBAL_USED_MEDIA_FILE.exists():
        try:
            return set(json.loads(GLOBAL_USED_MEDIA_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()

def save_global_used_media(used_set):
    try:
        # Keep a rolling limit of the last 600 items to avoid query starvation
        lst = list(used_set)
        if len(lst) > 600:
            lst = lst[-600:]
        GLOBAL_USED_MEDIA_FILE.write_text(json.dumps(lst), encoding="utf-8")
    except Exception:
        pass

MUSIC_TRACKS = {
    "cinematic": "https://raw.githubusercontent.com/tannerhelland/free-music/master/mp3/Reign%20of%20Anarchy.mp3",
    "epic": "https://raw.githubusercontent.com/tannerhelland/free-music/master/mp3/Defiance.mp3",
    "dramatic": "https://raw.githubusercontent.com/tannerhelland/free-music/master/mp3/Ominosity.mp3",
    "adventure": "https://raw.githubusercontent.com/tannerhelland/free-music/master/mp3/March%20of%20the%20Zargansk.mp3",
    "mystery": "https://raw.githubusercontent.com/tannerhelland/free-music/master/mp3/Assault%20on%20Mist%20Castle.mp3",
}


def update(job_id, step, progress, message):
    if job_id in jobs:
        current_progress = jobs[job_id].get("progress", 0)
        new_progress = max(0, min(100, int(progress)))
        if new_progress >= current_progress:
            jobs[job_id]["progress"] = new_progress
        jobs[job_id].update({
            "step": step,
            "message": message,
            "updatedAt": time.time(),
        })


def slugify(text):
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[-\s]+", "-", text)
    return text[:80] or "video"


def env_or(value, name):
    return (value or os.environ.get(name) or "").strip()


def ffmpeg_bin():
    if imageio_ffmpeg:
        return imageio_ffmpeg.get_ffmpeg_exe()
    path = shutil.which("ffmpeg")
    if path:
        return path
    raise RuntimeError("FFmpeg is not available. Run START.bat again so imageio-ffmpeg installs.")


def wikipedia_context(topic):
    candidates = [
        topic,
        re.sub(r"^\s*(history|story|rise|fall)\s+of\s+", "", topic, flags=re.I).strip(),
        topic.replace("Fifa", "FIFA"),
    ]
    try:
        for query in dict.fromkeys([c for c in candidates if c]):
            search = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "format": "json",
                    "utf8": 1,
                    "srlimit": 1,
                },
                headers={"User-Agent": "AI Video Agent/1.0"},
                timeout=15,
            )
            search.raise_for_status()
            hits = search.json().get("query", {}).get("search", [])
            page = hits[0]["title"] if hits else query
            summary = requests.get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/" + requests.utils.quote(page),
                timeout=15,
                headers={"User-Agent": "AI Video Agent/1.0"},
            )
            if summary.ok:
                data = summary.json()
                extract = data.get("extract", "")
                if extract:
                    return {
                        "title": data.get("title", topic),
                        "extract": extract,
                        "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
                    }
    except Exception:
        pass
    return {"title": topic, "extract": "", "url": ""}


def call_gemini(topic, style, format_name, scene_count, api_key, context, language="English"):
    prompt = f"""
Create a highly cinematic, dramatic, and poetic documentary script about: "{topic}" in {language}. If Urdu, write the narration in native Urdu Arabic script. If Hindi, write the narration in native Hindi Devanagari script. Visual search queries (the 'visual' property) must ALWAYS be written in English.
Format: {format_name}
Style: Cinematic Space/Historical Documentary (similar to the awe-inspiring, mysterious storytelling style of ReYOUniverse and Melodysheep).
Scene count: exactly {scene_count}

Use this factual context for information:
{context.get("extract", "")}

Return ONLY valid JSON:
{{
  "title": "A highly cinematic and poetic title for the documentary",
  "description": "Engaging, high-retention upload description",
  "keywords": ["keyword1", "keyword2"],
  "scenes": [
    {{
      "title": "short scene title",
      "narration": "cinematic narration text",
      "caption": "short dramatic on-screen caption",
      "visual": "cinematic stock video search query",
      "duration": 4
    }}
  ]
}}

CRITICAL DIRECTIVES FOR CINEMATIC MASTERPIECE SCRIPTWRITING:
1. DRAMATIC NARRATION STYLE & PACING:
   - The narration must read like a premium space or science film. Use poetic, grand, awe-inspiring, and serious language.
   - Use em-dashes (—), commas, and ellipses (...) strategically inside the narration to force the voice generator to make natural, dramatic pauses (e.g. "A quiet world... unaware of the looming shadow—until the sky itself caught fire.").
   - DO NOT lecture. DO NOT use academic or teaching phrases like "In this video", "Now let's examine", "As we know", "This happens because". Speak with absolute authority and drama.
   - Seamless Storytelling: The script must not be a collection of disconnected facts. Every scene's narration must flow continuously and organically into the next scene, building a single epic narrative arc.
   - Vary vocabulary: Do NOT repeat the main keywords, topic name, or phrases in consecutive scenes. Use pronouns and creative synonyms.
   - Keep narration extremely short, strictly between 6 to 10 words per scene so it can be spoken in under 4 seconds. Every scene duration must be between 3 to 5 seconds.

2. PUNCHY DRAMATIC CAPTIONS:
   - Captions are uppercase overlay text. They must be highly dramatic, punchy, and short (2 to 4 words max).
   - Use them like cinematic title cards (e.g. "AN UNSEEN FORCE", "THE IMPACT", "DAWN OF EXTINCTION"). Do not repeat the narration text.

3. CINEMATIC VISUAL SEARCH QUERIES & GRAPHICS:
   - Search queries are used to download stock videos (Pexels) or generate AI images. They must describe concrete, visually stunning, and dramatic scenes.
   - ALWAYS include cinematic descriptors like: 'cinematic lighting', 'slow motion', 'epic dynamic panning', 'extreme close up', 'photorealistic CGI animation', 'dramatic mist'.
   - DO NOT search for abstract concepts (like 'danger', 'history', 'extinction'). Search for physical things: e.g. 'extreme close up eye of dinosaur dilating dramatic lighting', 'glowing asteroid entering dark atmosphere fire trail CGI'.
   - MAP DIRECTIVE: If the scene discusses a country, geographical region, or shifting borders, specify a map prompt. e.g. "3D satellite map of India with glowing borders, drone flyover cartography graphic style", "vintage historical paper map of Europe panning shot".
   - DATA DIRECTIVE: If the scene discusses numbers, statistics, stock markets, growth, or real-time data, specify a high-tech data chart prompt. e.g. "holographic stock market financial data chart glowing animation", "futuristic tech world map connections database graphics".
   - Avoid specific historical names or places in queries (Pexels doesn't know them). Use generic visual equivalents (e.g. 'ancient king crown male' instead of 'Mehmet Fateh', 'ancient castle fortress burning' instead of 'Constantinople').
   - Always specify gender for characters in the visual query to prevent incorrect matches.
"""
    models = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
    last_error = ""
    for model in models:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            res = requests.post(
                url,
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.75,
                        "maxOutputTokens": 8192,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=60,
            )
            if not res.ok:
                print(f"[Gemini Error] Model {model} failed with status {res.status_code}: {res.text}")
                last_error = res.text[:300]
                continue
            raw = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"```json|```", "", raw).strip())
        except Exception as exc:
            print(f"[Gemini Exception] Model {model} threw exception: {exc}")
            last_error = str(exc)
    raise RuntimeError("Gemini script generation failed: " + last_error)


DEFAULT_GROQ_API_KEY = "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"


def call_groq(prompt, api_key=None):
    key = api_key or DEFAULT_GROQ_API_KEY
    if not key:
        return None
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a backend JSON API. You MUST return ONLY a raw JSON object matching the requested schema. "
                            "Do NOT include any preamble, markdown code blocks (like ```json), thinking, explanation, or conversational text. "
                            "Start your response with '{' and end with '}'."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.75,
                "response_format": {"type": "json_object"}
            },
            timeout=35
        )
        if res.ok:
            raw = res.json()["choices"][0]["message"]["content"].strip()
            raw = re.sub(r"^```json\s*", "", raw, flags=re.I)
            raw = re.sub(r"\s*```$", "", raw)
            raw = raw.strip()
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1:
                parsed = json.loads(raw[start:end+1])
                return parsed
    except Exception as e:
        print("Groq completion failed:", e)
    return None


def call_pollinations_text(prompt):
    try:
        res = requests.post(
            "https://text.pollinations.ai/",
            json={
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a backend JSON API. You MUST return ONLY a raw JSON object matching the requested schema. "
                            "Do NOT include any preamble, markdown code blocks (like ```json), thinking, explanation, or conversational text. "
                            "Start your response with '{' and end with '}'."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                "model": "openai",
                "jsonMode": True
            },
            timeout=30
        )
        if res.ok:
            raw = res.text.strip()
            raw = re.sub(r"^```json\s*", "", raw, flags=re.I)
            raw = re.sub(r"\s*```$", "", raw)
            raw = raw.strip()
            
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1:
                parsed = json.loads(raw[start:end+1])
                if "scenes" in parsed and isinstance(parsed["scenes"], list) and len(parsed["scenes"]) >= 3:
                    return parsed
                elif "chapters" in parsed and isinstance(parsed["chapters"], list) and len(parsed["chapters"]) > 0:
                    return parsed
    except Exception as e:
        print("Pollinations text error:", e)
    return None


def generate_outline(topic, style, duration_mins, api_key, language="English"):
    num_chapters = max(1, int(duration_mins))
    prompt = f"""
You are a world-class documentary director. We are making a premium, cinematic, highly dramatic, and immersive {duration_mins}-minute documentary about: "{topic}".
Language: Write the outline (titles, descriptions, and chapters) in {language}. If Urdu, write in native Urdu Arabic script. If Hindi, write in native Hindi Devanagari script.
Style: Cinematic Documentary (like ReYOUniverse / Melodysheep style, featuring epic narration, deep cosmic questions, and dramatic storytelling).
We need to divide this documentary into exactly {num_chapters} chapters.

Return ONLY a valid JSON object matching this schema:
{{
  "title": "A highly cinematic and poetic title for the documentary",
  "chapters": [
    {{
      "number": 1,
      "title": "A dramatic, cinematic chapter title",
      "description": "A compelling description summarizing the dramatic storytelling arc of this chapter"
    }}
  ]
}}
"""
    if api_key:
        try:
            res = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.75,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=60,
            )
            if res.ok:
                raw = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(re.sub(r"```json|```", "", raw).strip())
                if "chapters" in parsed and isinstance(parsed["chapters"], list) and len(parsed["chapters"]) > 0:
                    return parsed
        except Exception as e:
            print("Gemini outline failed:", e)
            
    outline = call_groq(prompt)
    if outline and isinstance(outline.get("chapters"), list) and len(outline["chapters"]) > 0:
        return outline
            
    outline = call_pollinations_text(prompt)
    if outline and isinstance(outline.get("chapters"), list) and len(outline["chapters"]) > 0:
        return outline
        
    chapters = []
    for i in range(1, num_chapters + 1):
        chapters.append({
            "number": i,
            "title": f"Part {i}: The Story of {topic}",
            "description": f"Exploring the historical and cultural impact of {topic} in part {i}."
        })
    return {
        "title": f"The Story of {topic}",
        "chapters": chapters
    }


def clean_chapter_narration(script, chapter_title, chapter_num):
    import re
    if not script or not isinstance(script, dict) or "scenes" not in script:
        return script
    scenes = script.get("scenes", [])
    if not isinstance(scenes, list):
        return script
    for scene in scenes:
        if not isinstance(scene, dict) or "narration" not in scene:
            continue
        narration = str(scene["narration"]).strip()
        cleaned = re.sub(r'(?i)^\s*(chapter|part|section)\s+\d+[\s:\-–—\.]*', '', narration)
        norm_title = re.sub(r'[^\w\s]', '', chapter_title).strip().lower()
        if norm_title:
            words = cleaned.split()
            title_words = norm_title.split()
            if len(words) >= len(title_words):
                norm_start = re.sub(r'[^\w\s]', '', " ".join(words[:len(title_words)])).strip().lower()
                if norm_start == norm_title:
                    cleaned = " ".join(words[len(title_words):])
                    cleaned = re.sub(r'^[\s:\-–—\.,!\?]*', '', cleaned)
        scene["narration"] = re.sub(r'\s+', ' ', cleaned).strip()
    return script


def generate_chapter_script(topic, doc_title, chapter, chapter_num, scene_count, style, format_name, api_key, language="English", prev_narration="", full_outline_desc=""):
    memory_context = ""
    if prev_narration:
        memory_context = f"""
PREVIOUS CHAPTER NARRATION (DO NOT REPEAT WORDS/IDEAS):
Here is how the narration ended in the previous chapters:
"... {prev_narration}"
You MUST continue the story organically and chronologically from this exact point. Do NOT repeat any facts, events, years, descriptions, or introductory phrases from this previous text. Vary your vocabulary and focus strictly on new details.
"""

    outline_context = ""
    if full_outline_desc:
        outline_context = f"""
FULL DOCUMENTARY OUTLINE (FOR TIMELINE CONTEXT):
Here is the complete chronological outline of the film:
{full_outline_desc}
Use this to understand where Chapter {chapter_num} sits. Do NOT reference future events that belong in later chapters.
"""

    # Dynamic Style Directives
    style_directives = ""
    if style == "explainer":
        style_directives = """
STYLE DIRECTIVES: Clear, Educational, Engaging Explainer.
- Break down complex ideas into simple terms.
- Use analogies and clear, friendly narration.
- Narrate with high-energy educational authority.
- The script must explain the facts, mechanisms, and details of the topic clearly.
"""
    elif style == "story":
        style_directives = """
STYLE DIRECTIVES: Immersive, Dramatic Narrative Story.
- Focus on character actions, thoughts, and emotional beats.
- Build tension, suspense, or emotional resonance scene-by-scene.
- Speak in a rich, literary storytelling voice.
"""
    elif style == "news":
        style_directives = """
STYLE DIRECTIVES: Journalistic, Direct News Report.
- Objective, fact-first, clear reporting.
- Answer the who, what, where, when, and why immediately.
- Use a professional, neutral news anchor tone.
"""
    else:  # documentary
        style_directives = """
STYLE DIRECTIVES: Premium, Fact-Rich Documentary (Chronological and Authoritative).
- Speak with absolute authority and historical gravity.
- Recount exact names, dates, key figures, and chronological events.
- Do NOT use vague or empty poetic fluff (e.g. avoid repeating "a world of shadows...", "ancient secrets..."). Speak in concrete, dense historical facts.
"""

    topic_specificity = f"""
CRITICAL DIRECTIVE ON TOPIC SPECIFICITY:
- The script MUST be highly specific to the topic: "{topic}".
- Avoid generic filler. If the topic is a show review, mention actual characters, plots, events, and season 3 elements. If it is a historical figure, mention specific deeds, places, and events.
- Every single sentence of the narration must contain concrete information, facts, or analysis related directly to "{topic}".
"""

    prompt = f"""
{outline_context}
{memory_context}

Create a premium script for Chapter {chapter_num} of the project.
Documentary Title: "{doc_title}"
Topic: "{topic}"
Chapter Title: "{chapter['title']}"
Description of this chapter: "{chapter['description']}"
Scene count: exactly {scene_count}
Language: Write the script narration in {language}. If Urdu, write the narration in native Urdu Arabic script. If Hindi, write the narration in native Hindi Devanagari script. Visual search queries (the 'visual' property) must ALWAYS be written in English.

{style_directives}
{topic_specificity}

Return ONLY valid JSON:
{{
  "scenes": [
    {{
      "title": "short scene title",
      "narration": "cinematic narration text",
      "caption": "short dramatic on-screen caption",
      "visual": "cinematic stock video search query",
      "duration": 4
    }}
  ]
}}

CRITICAL DIRECTIVES FOR CINEMATIC MASTERPIECE SCRIPTWRITING:
1. DRAMATIC NARRATION STYLE & PACING:
   - Natural flowing speech: Write in complete, continuous, and natural flowing sentences. Do NOT use excess ellipses (...) or em-dashes (—) that break the speech flow uncomfortably. The narration should sound like a professional voiceover artist delivering a masterclass, speaking in complete thoughts.
   - DO NOT lecture. DO NOT use academic or teaching phrases like "In this video", "Now let's examine", "As we know", "This happens because". Speak with absolute authority and drama.
   - Seamless Storytelling: Every scene's narration must flow continuously and organically into the next scene, building a single epic narrative arc.
   - Vary vocabulary: Do NOT repeat the main keywords, topic name, or phrases in consecutive scenes. Use pronouns and creative synonyms.
   - Keep narration between 18 to 28 words per scene to allow the voiceover to speak complete, rich sentences.
   - ABSOLUTE BAN ON REPETITIVE INTRODUCTIONS: This is Chapter {chapter_num}. Do NOT write an introduction. Do NOT state who the subject is (e.g., "Osama Bin Laden was..."). Start directly with the actions and details of this chapter.
   - NEVER INCLUDE CHAPTER TITLES OR NUMBERS IN THE NARRATION: Do NOT write the words "Chapter {chapter_num}" or "{chapter['title']}" in the narration text. The voiceover generator will read whatever text you write out loud, so if you write chapter titles, the narrator will read them, which sounds extremely unprofessional. The narration must be 100% pure storytelling without any chapter/part announcements.
   - SEAMLESS CHAPTER FLOW: This chapter is a direct continuation of a single, massive, chronological documentary. Treat the division into chapters purely as a silent visual separator. The narration must flow seamlessly from the last word of the previous chapter to the first word of this chapter, as if it were a single continuous voiceover script. There should be NO transition phrases, no introductions, and no conclusions. It is just the next paragraph in the book.
   - DO NOT DESCRIBE THE VISUALS: The narrator is a master storyteller, not an audio description robot. The narrator must NEVER describe what is happening in the stock video (e.g., do NOT write "We see a tank...", "A close-up of a map...", "A man walks..."). The narration must tell the historical, political, or cosmic story itself. The video clips are just background illustrations.

2. PUNCHY DRAMATIC CAPTIONS:
   - Captions are uppercase overlay text. They must be highly dramatic, punchy, and short (2 to 4 words max).
   - Use them like cinematic title cards (e.g. "AN UNSEEN FORCE", "THE IMPACT", "DAWN OF EXTINCTION"). Do not repeat the narration text.

3. CINEMATIC VISUAL SEARCH QUERIES:
   - Search queries are used to download stock videos (Pexels) or generate AI images. They must describe concrete, visually stunning, and dramatic scenes.
   - ALWAYS include cinematic descriptors like: 'cinematic lighting', 'slow motion', 'epic dynamic panning', 'extreme close up', 'photorealistic CGI animation', 'dramatic mist'.
   - DO NOT search for abstract concepts (like 'danger', 'history', 'extinction'). Search for physical things: e.g. 'extreme close up eye of dinosaur dilating dramatic lighting', 'glowing asteroid entering dark atmosphere fire trail CGI', 'molten lava flowing dark smoke drone shot'.
   - Avoid specific historical names or places in queries (Pexels doesn't know them). Use generic visual equivalents (e.g. 'ancient king crown male' instead of 'Mehmet Fateh', 'ancient castle fortress burning' instead of 'Constantinople').
   - Always specify gender for characters in the visual query to prevent incorrect matches.
"""
    if api_key:
        try:
            res = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.75,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=60,
            )
            res.raise_for_status()
            raw = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(re.sub(r"```json|```", "", raw).strip())
            if "scenes" in parsed and isinstance(parsed["scenes"], list) and len(parsed["scenes"]) >= 3:
                return normalize_script(clean_chapter_narration(parsed, chapter['title'], chapter_num), topic, format_name)
        except Exception as e:
            print(f"[Fallback Warning] Gemini chapter script failed: {e}. Falling back to Groq/Pollinations.")
            
    script = call_groq(prompt)
    if script and isinstance(script.get("scenes"), list) and len(script["scenes"]) >= 3:
        return normalize_script(clean_chapter_narration(script, chapter['title'], chapter_num), topic, format_name)
            
    script = call_pollinations_text(prompt)
    if script and isinstance(script.get("scenes"), list) and len(script["scenes"]) >= 3:
        return normalize_script(clean_chapter_narration(script, chapter['title'], chapter_num), topic, format_name)
        
    return normalize_script(clean_chapter_narration(fallback_script(f"{topic} - {chapter['title']}", style, format_name, scene_count, {}), chapter['title'], chapter_num), topic, format_name)


def fallback_script(topic, style, format_name, scene_count, context):
    subject = context.get("title") or topic
    extract = context.get("extract") or ""
    
    extract = re.sub(r"\s+", " ", extract)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", extract) if len(s.strip().split()) > 4]
    
    if len(sentences) < scene_count:
        themes = [
            f"Discovering the untold story of {subject}.",
            f"How {subject} changed the course of history forever.",
            f"The key moments that defined the legacy of {subject}.",
            f"Uncovering the hidden secrets behind {subject}.",
            f"The dramatic rise and unexpected challenges of {subject}.",
            f"Why the world will never forget the impact of {subject}.",
            f"The fascinating details that experts rarely talk about.",
            f"Looking back at the foundation of {subject}.",
            f"A turning point that shifted everything we knew.",
            f"The lasting influence of {subject} on modern society."
        ]
        while len(sentences) < scene_count:
            sentences.append(themes[len(sentences) % len(themes)])
            
    scenes = []
    for i in range(scene_count):
        sentence = sentences[i % len(sentences)]
        words = sentence.split()
        if len(words) > 8:
            narration = " ".join(words[:8]) + "."
        else:
            narration = sentence
            
        caption = caption_from_text(narration)
        
        clean_words = []
        for w in narration.split():
            clean_w = w.strip(",.;:!?()\"'").lower()
            if len(clean_w) > 3 and clean_w not in ["about", "their", "there", "would", "could", "should", "history", "subject", "story", "mehmed", "mehmet", "fateh", "constantinople", "ottoman", "conqueror"]:
                clean_words.append(clean_w)
        
        if clean_words:
            visual = " ".join(clean_words[:3])
        else:
            visual = "medieval castle"
            
        scenes.append({
            "title": f"Beat {i+1}",
            "narration": narration,
            "caption": caption,
            "visual": visual,
            "duration": 4
        })
        
    return {
        "title": subject,
        "description": f"A professional documentary about {subject}.",
        "keywords": [subject, style, format_name],
        "scenes": scenes
    }


def caption_from_text(text):
    text = re.sub(r"^[A-Z][A-Za-z\s]+[.:]\s*", "", text).strip()
    words = [w.strip(",.;:!?") for w in text.split() if w.strip(",.;:!?")]
    return " ".join(words[:5]).upper()


def visual_query(subject, angle, seed=""):
    lower = f"{subject} {angle}".lower()
    if any(word in lower for word in ["fifa", "football", "soccer", "world cup"]):
        return seed or "football stadium"
    if any(word in lower for word in ["rome", "roman", "empire", "ancient"]):
        roman = {
            "beginning": "ancient rome ruins",
            "spark": "roman statue",
            "world": "roman colosseum",
            "legend": "ancient sculpture",
            "stage": "roman columns",
            "legacy": "historic museum",
        }
        for key, value in roman.items():
            if key in lower:
                return value
        return "ancient rome"
    return seed or subject


def generate_script(topic, style, format_name, scene_count, api_key, language="English"):
    context = wikipedia_context(topic)
    
    # Dynamic Style Directives
    style_directives = ""
    if style == "explainer":
        style_directives = """
STYLE DIRECTIVES: Clear, Educational, Engaging Explainer.
- Break down complex ideas into simple terms.
- Use analogies and clear, friendly narration.
- Narrate with high-energy educational authority.
- The script must explain the facts, mechanisms, and details of the topic clearly.
"""
    elif style == "story":
        style_directives = """
STYLE DIRECTIVES: Immersive, Dramatic Narrative Story.
- Focus on character actions, thoughts, and emotional beats.
- Build tension, suspense, or emotional resonance scene-by-scene.
- Speak in a rich, literary storytelling voice.
"""
    elif style == "news":
        style_directives = """
STYLE DIRECTIVES: Journalistic, Direct News Report.
- Objective, fact-first, clear reporting.
- Answer the who, what, where, when, and why immediately.
- Use a professional, neutral news anchor tone.
"""
    else:  # documentary
        style_directives = """
STYLE DIRECTIVES: Premium, Fact-Rich Documentary (Chronological and Authoritative).
- Speak with absolute authority and historical gravity.
- Recount exact names, dates, key figures, and chronological events.
- Do NOT use vague or empty poetic fluff (e.g. avoid repeating "a world of shadows...", "ancient secrets..."). Speak in concrete, dense historical facts.
"""

    topic_specificity = f"""
CRITICAL DIRECTIVE ON TOPIC SPECIFICITY:
- The script MUST be highly specific to the topic: "{topic}".
- Avoid generic filler. If the topic is a show review, mention actual characters, plots, events, and season 3 elements. If it is a historical figure, mention specific deeds, places, and events.
- Every single sentence of the narration must contain concrete information, facts, or analysis related directly to "{topic}".
"""

    prompt = f"""
Create a premium script for the project about: "{topic}" in {language}.
Format: {format_name}
Scene count: exactly {scene_count}
Language: Write the script narration in {language}. If Urdu, write the narration in native Urdu Arabic script. If Hindi, write the narration in native Hindi Devanagari script. Visual search queries (the 'visual' property) must ALWAYS be written in English.

Use this factual context for information:
{context.get("extract", "")}

{style_directives}
{topic_specificity}

Return ONLY valid JSON:
{{
  "title": "A highly cinematic and poetic title for the documentary",
  "description": "Engaging, high-retention upload description",
  "keywords": ["keyword1", "keyword2"],
  "scenes": [
    {{
      "title": "short scene title",
      "narration": "cinematic narration text",
      "caption": "short dramatic on-screen caption",
      "visual": "cinematic stock video search query",
      "duration": 4
    }}
  ]
}}

CRITICAL DIRECTIVES FOR CINEMATIC MASTERPIECE SCRIPTWRITING:
1. DRAMATIC NARRATION STYLE & PACING:
   - Natural flowing speech: Write in complete, continuous, and natural flowing sentences. Do NOT use excess ellipses (...) or em-dashes (—) that break the speech flow uncomfortably. The narration should sound like a professional voiceover artist delivering a masterclass, speaking in complete thoughts.
   - DO NOT lecture. DO NOT use academic or teaching phrases like "In this video", "Now let's examine", "As we know", "This happens because". Speak with absolute authority and drama.
   - Seamless Storytelling: Every scene's narration must flow continuously and organically into the next scene, building a single epic narrative arc.
   - Vary vocabulary: Do NOT repeat the main keywords, topic name, or phrases in consecutive scenes. Use pronouns and creative synonyms.
   - Keep narration between 18 to 28 words per scene to allow the voiceover to speak complete, rich sentences.
   - DO NOT DESCRIBE THE VISUALS: The narrator is a master storyteller, not an audio description robot. The narrator must NEVER describe what is happening in the stock video (e.g., do NOT write "We see a tank...", "A close-up of a map...", "A man walks..."). The narration must tell the historical, political, or cosmic story itself. The video clips are just background illustrations.

2. PUNCHY DRAMATIC CAPTIONS:
   - Captions are uppercase overlay text. They must be highly dramatic, punchy, and short (2 to 4 words max).
   - Use them like cinematic title cards (e.g. "AN UNSEEN FORCE", "THE IMPACT", "DAWN OF EXTINCTION"). Do not repeat the narration text.

3. CINEMATIC VISUAL SEARCH QUERIES & GRAPHICS:
   - Search queries are used to download stock videos (Pexels) or generate AI images. They must describe concrete, visually stunning, and dramatic scenes.
   - ALWAYS include cinematic descriptors like: 'cinematic lighting', 'slow motion', 'epic dynamic panning', 'extreme close up', 'photorealistic CGI animation', 'dramatic mist'.
   - DO NOT search for abstract concepts (like 'danger', 'history', 'extinction'). Search for physical things: e.g. 'extreme close up eye of dinosaur dilating dramatic lighting', 'glowing asteroid entering dark atmosphere fire trail CGI'.
   - MAP DIRECTIVE: If the scene discusses a country, geographical region, or shifting borders, specify a map prompt. e.g. "3D satellite map of India with glowing borders, drone flyover cartography graphic style", "vintage historical paper map of Europe panning shot".
   - DATA DIRECTIVE: If the scene discusses numbers, statistics, stock markets, growth, or real-time data, specify a high-tech data chart prompt. e.g. "holographic stock market financial data chart glowing animation", "futuristic tech world map connections database graphics".
   - Avoid specific historical names or places in queries (Pexels doesn't know them). Use generic visual equivalents (e.g. 'ancient king crown male' instead of 'Mehmet Fateh', 'ancient castle fortress burning' instead of 'Constantinople').
   - Always specify gender for characters in the visual query to prevent incorrect matches.
"""

    if api_key:
        try:
            script = call_gemini(topic, style, format_name, scene_count, api_key, context, language)
            if script and isinstance(script.get("scenes"), list) and len(script["scenes"]) >= 3:
                script["source"] = context.get("url", "")
                return normalize_script(script, topic, format_name)
        except Exception as e:
            print(f"[Fallback Warning] Gemini call failed: {e}. Falling back to Groq/Pollinations.")
            
    script = call_groq(prompt)
    if script and isinstance(script.get("scenes"), list) and len(script["scenes"]) >= 3:
        script["source"] = context.get("url", "")
        return normalize_script(script, topic, format_name)
            
    script = call_pollinations_text(prompt)
    if script and isinstance(script.get("scenes"), list) and len(script["scenes"]) >= 3:
        script["source"] = context.get("url", "")
        return normalize_script(script, topic, format_name)
        
    return normalize_script(fallback_script(topic, style, format_name, scene_count, context), topic, format_name)


def normalize_script(script, topic, format_name):
    script.setdefault("title", topic)
    script.setdefault("description", "")
    script.setdefault("keywords", [topic])
    scenes = script.get("scenes", [])
    clean = []
    for idx, scene in enumerate(scenes, 1):
        narration = str(scene.get("narration", "")).strip()
        if not narration:
            narration = f"{topic} scene {idx}."
            
        caption = str(scene.get("caption") or "").strip().upper()
        if not caption:
            caption = caption_from_text(narration)
            
        visual = str(scene.get("visual") or "").strip()
        if not visual:
            visual = topic
            
        clean.append({
            "title": str(scene.get("title") or f"Scene {idx}"),
            "narration": narration,
            "caption": caption,
            "visual": visual,
            "duration": int(scene.get("duration") or 4)
        })
        
    script["scenes"] = clean
    return script


def download_pexels_image(query, dest, api_key, vertical, used_urls=None, topic=None):
    api_key = (api_key or "").strip() or DEFAULT_PEXELS_API_KEY
    if not api_key:
        return False
    try:
        photo_link = None
        with pexels_lock:
            import random
            page = random.randint(1, 4)
            res = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": api_key},
                params={
                    "query": query,
                    "orientation": "portrait" if vertical else "landscape",
                    "per_page": 15,
                    "page": page
                },
                timeout=15,
            )
            photos = []
            if res.status_code == 200:
                photos = res.json().get("photos", [])
                
            if not photos:
                res = requests.get(
                    "https://api.pexels.com/v1/search",
                    headers={"Authorization": api_key},
                    params={
                        "query": query,
                        "orientation": "portrait" if vertical else "landscape",
                        "per_page": 15,
                        "page": 1
                    },
                    timeout=15,
                )
                if res.status_code == 200:
                    photos = res.json().get("photos", [])
            
            # If all results from the specific query are already in used_urls,
            # and topic is provided, try searching using the broader topic instead!
            has_unused = False
            if photos:
                for p in photos:
                    src = p["src"].get("large") or p["src"].get("large2x") or p["src"]["original"]
                    if used_urls is None or src not in used_urls:
                        has_unused = True
                        break
            if not has_unused and topic and query.lower() != topic.lower():
                print(f"Pexels specific image query '{query}' exhausted. Trying broader topic '{topic}'...")
                # Search broader topic on a random page as well
                page = random.randint(1, 3)
                res = requests.get(
                    "https://api.pexels.com/v1/search",
                    headers={"Authorization": api_key},
                    params={
                        "query": topic,
                        "orientation": "portrait" if vertical else "landscape",
                        "per_page": 30,
                        "page": page
                    },
                    timeout=15,
                )
                if res.status_code == 200:
                    photos = res.json().get("photos", [])
                if not photos:
                    res = requests.get(
                        "https://api.pexels.com/v1/search",
                        headers={"Authorization": api_key},
                        params={
                            "query": topic,
                            "orientation": "portrait" if vertical else "landscape",
                            "per_page": 30,
                            "page": 1
                        },
                        timeout=15,
                    )
                    if res.status_code == 200:
                        photos = res.json().get("photos", [])

            if photos:
                best_photo = None
                for photo in photos:
                    src = photo["src"].get("large") or photo["src"].get("large2x") or photo["src"]["original"]
                    if used_urls is not None and src in used_urls:
                        continue
                    best_photo = src
                    if used_urls is not None:
                        used_urls.add(src)
                    break
                if not best_photo:
                    best_photo = photos[0]["src"].get("large") or photos[0]["src"].get("large2x") or photos[0]["src"]["original"]
                photo_link = best_photo
                
        if not photo_link:
            return False
            
        img = requests.get(photo_link, timeout=20)
        img.raise_for_status()
        tmp_dest = dest.with_suffix(".tmp")
        tmp_dest.write_bytes(img.content)
        if tmp_dest.exists():
            tmp_dest.replace(dest)
        return True
    except Exception:
        return False


def download_pexels_video(query, dest, api_key, vertical, used_urls=None, topic=None):
    api_key = (api_key or "").strip() or DEFAULT_PEXELS_API_KEY
    if not api_key:
        return False
    try:
        video_link = None
        video_url = None
        
        with pexels_lock:
            import random
            page = random.randint(1, 4)
            res = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": api_key},
                params={
                    "query": query,
                    "orientation": "portrait" if vertical else "landscape",
                    "per_page": 15,
                    "size": "medium",
                    "page": page
                },
                timeout=15,
            )
            videos = []
            if res.status_code == 200:
                videos = res.json().get("videos", [])
                
            if not videos:
                res = requests.get(
                    "https://api.pexels.com/videos/search",
                    headers={"Authorization": api_key},
                    params={
                        "query": query,
                        "orientation": "portrait" if vertical else "landscape",
                        "per_page": 15,
                        "size": "medium",
                        "page": 1
                    },
                    timeout=15,
                )
                if res.status_code == 200:
                    videos = res.json().get("videos", [])
            
            # If all results from the specific query are already in used_urls,
            # and topic is provided, try searching using the broader topic instead!
            has_unused = False
            if videos:
                for v in videos:
                    v_url = v["url"]
                    if used_urls is None or v_url not in used_urls:
                        has_unused = True
                        break
            if not has_unused and topic and query.lower() != topic.lower():
                print(f"Pexels specific video query '{query}' exhausted. Trying broader topic '{topic}'...")
                page = random.randint(1, 3)
                res = requests.get(
                    "https://api.pexels.com/videos/search",
                    headers={"Authorization": api_key},
                    params={
                        "query": topic,
                        "orientation": "portrait" if vertical else "landscape",
                        "per_page": 30,
                        "size": "medium",
                        "page": page
                    },
                    timeout=15,
                )
                if res.status_code == 200:
                    videos = res.json().get("videos", [])
                if not videos:
                    res = requests.get(
                        "https://api.pexels.com/videos/search",
                        headers={"Authorization": api_key},
                        params={
                            "query": topic,
                            "orientation": "portrait" if vertical else "landscape",
                            "per_page": 30,
                            "size": "medium",
                            "page": 1
                        },
                        timeout=15,
                    )
                    if res.status_code == 200:
                        videos = res.json().get("videos", [])

            if videos:
                best_file = None
                target_width = 720 if vertical else 1280
                target_height = 1280 if vertical else 720
                for video in videos:
                    v_url = video["url"]
                    if used_urls is not None and v_url in used_urls:
                        continue
                        
                    best_vf = None
                    best_score = -1000000
                    for vf in video.get("video_files", []):
                        if vf.get("file_type") != "video/mp4":
                            continue
                        width = vf.get("width") or 0
                        height = vf.get("height") or 0
                        
                        is_correct_orientation = (height >= width) if vertical else (width >= height)
                        orientation_score = 5000 if is_correct_orientation else 0
                        
                        diff = abs(width - target_width) + abs(height - target_height)
                        score = orientation_score - diff
                        
                        if score > best_score:
                            best_vf = vf
                            best_score = score
                    if best_vf:
                        best_file = best_vf
                        video_url = v_url
                        break
                        
                if not best_file and videos:
                    # Check if first video is in used_urls, if so try others
                    for video in videos:
                        v_url = video["url"]
                        if used_urls is not None and v_url in used_urls:
                            continue
                        for vf in video.get("video_files", []):
                            if vf.get("file_type") == "video/mp4":
                                best_file = vf
                                video_url = v_url
                                break
                        if best_file:
                            break
                    # absolute fallback
                    if not best_file:
                        for vf in videos[0].get("video_files", []):
                            if vf.get("file_type") == "video/mp4":
                                best_file = vf
                                video_url = videos[0]["url"]
                                break
                
                if best_file:
                    video_link = best_file["link"]
                    if used_urls is not None and video_url:
                        used_urls.add(video_url)
                        
        if not video_link:
            return False
            
        clip = requests.get(video_link, timeout=30)
        clip.raise_for_status()
        tmp_dest = dest.with_suffix(".tmp")
        tmp_dest.write_bytes(clip.content)
        if tmp_dest.exists():
            tmp_dest.replace(dest)
        return True
    except Exception:
        return False


def download_pollinations_image(prompt, dest, vertical, topic=""):
    width, height = (720, 1280) if vertical else (1280, 720)
    full_prompt = prompt
    if topic and topic.lower() not in prompt.lower():
        full_prompt = f"{topic}: {prompt}"
    url = "https://image.pollinations.ai/prompt/" + requests.utils.quote(
        f"{full_prompt}, cinematic, realistic, high quality, no text"
    )
    import random
    seed = random.randint(1, 10000000)
    for attempt in range(5):
        try:
            res = requests.get(url, params={"width": width, "height": height, "nologo": "true", "seed": seed}, timeout=35)
            if res.status_code == 429:
                time.sleep(2 + attempt * 2.5 + random.random() * 2)
                continue
            res.raise_for_status()
            tmp_dest = dest.with_suffix(".tmp")
            tmp_dest.write_bytes(res.content)
            if tmp_dest.exists():
                tmp_dest.replace(dest)
            return
        except Exception as exc:
            if attempt == 4:
                raise exc
            time.sleep(1 + attempt * 2 + random.random())


def write_google_tts(text, dest_wav, voice_name="en-US-AriaNeural"):
    # Split text into chunks of 150 characters to stay within Google Translate's limit
    words = text.split()
    chunks = []
    current = []
    current_len = 0
    for w in words:
        if current_len + len(w) + 1 > 180:
            chunks.append(" ".join(current))
            current = [w]
            current_len = len(w)
        else:
            current.append(w)
            current_len += len(w) + 1
    if current:
        chunks.append(" ".join(current))
        
    import tempfile
    import requests
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    lang_code = "en"
    voice_lower = str(voice_name).lower()
    if voice_lower.startswith("ur"):
        lang_code = "ur"
    elif voice_lower.startswith("hi"):
        lang_code = "hi"
        
    combined_mp3_data = b""
    for chunk in chunks:
        url = f"https://translate.google.com/translate_tts?ie=UTF-8&tl={lang_code}&client=tw-ob&q={requests.utils.quote(chunk)}"
        res = requests.get(url, headers=headers, timeout=20)
        res.raise_for_status()
        combined_mp3_data += res.content
        
    # Write to a temp MP3 file
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(combined_mp3_data)
        temp_mp3 = Path(f.name)
        
    try:
        ffmpeg = ffmpeg_bin()
        subprocess.run([
            ffmpeg, "-y", "-i", str(temp_mp3), "-ar", "44100", "-ac", "2", str(dest_wav)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15, creationflags=SUBPROCESS_FLAGS)
    finally:
        if temp_mp3.exists():
            try:
                temp_mp3.unlink()
            except Exception:
                pass


def write_tts_wav(text, dest, voice="en-US-AriaNeural", rate="+3%"):
    voice = voice or os.environ.get("EDGE_TTS_VOICE", "en-US-AriaNeural")
    tmp_mp3 = dest.with_suffix(".mp3")
    try:
        # Run edge-tts via subprocess (python -m edge_tts) to avoid all asyncio thread issues on Windows
        cmd = [
            sys.executable, "-m", "edge_tts",
            "--voice", voice,
            "--text", text,
            f"--rate={rate}",
            "--write-media", str(tmp_mp3)
        ]
        subprocess.run(cmd, check=True, timeout=25, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
        
        # Convert MP3 to WAV using ffmpeg to a temporary path
        tmp_wav = dest.with_suffix(".tmp.wav")
        subprocess.run([
            ffmpeg_bin(), "-y", "-i", str(tmp_mp3), "-ar", "44100", "-ac", "2", str(tmp_wav)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15, creationflags=SUBPROCESS_FLAGS)
        if tmp_wav.exists():
            tmp_wav.replace(dest)
        
        # Clean up temporary MP3
        if tmp_mp3.exists():
            try:
                tmp_mp3.unlink()
            except Exception:
                pass
    except Exception as exc:
        print("Edge TTS subprocess failed, falling back to Google Translate TTS:", exc)
        try:
            write_google_tts(text, dest, voice)
        except Exception as e2:
            print("Google TTS fallback failed, trying Windows SpeechSynthesizer or silent audio fallback:", e2)
            if sys.platform == "win32":
                text_file = dest.with_suffix(".txt")
                try:
                    text_file.write_text(text, encoding="utf-8")
                    tmp_wav = dest.with_suffix(".tmp.wav")
                    ps = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = 1
$synth.Volume = 100
$text = Get-Content -Raw -Encoding UTF8 '{str(text_file).replace("'", "''")}'
$synth.SetOutputToWaveFile('{str(tmp_wav).replace("'", "''")}')
$synth.Speak($text)
$synth.Dispose()
"""
                    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20, creationflags=SUBPROCESS_FLAGS)
                    if tmp_wav.exists():
                        tmp_wav.replace(dest)
                        return
                except Exception as ps_exc:
                    print("PowerShell TTS also failed:", ps_exc)
                finally:
                    if text_file.exists():
                        try:
                            text_file.unlink()
                        except Exception:
                            pass
            # Final safety fallback: write silent wav of 4 seconds to prevent pipeline crashes
            write_silent_wav(4.0, dest)


def wav_duration(path):
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


def srt_time(seconds):
    ms = int((seconds - int(seconds)) * 1000)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def split_subtitles(text, start, duration, index):
    words = str(text).split()
    if not words:
        return [], index
        
    # Split into Netflix style subtitles of exactly 6 words (or fewer for the last chunk)
    chunk_size = 6
    chunks = [words[i:i + chunk_size] for i in range(0, len(words), chunk_size)]
    
    total_words = len(words)
    rows = []
    current_start = start
    for chunk in chunks:
        phrase = " ".join(chunk)
        chunk_word_count = len(chunk)
        # Calculate proportional duration for this chunk
        chunk_dur = duration * (chunk_word_count / total_words)
        current_end = current_start + chunk_dur
        rows.append(f"{index}\n{srt_time(current_start)} --> {srt_time(current_end)}\n{phrase}\n\n")
        index += 1
        current_start = current_end
        
    return rows, index


def render_scene(ffmpeg, media, audio, out, vertical, duration, resolution="1080p", caption="", scene_index=1):
    if resolution == "4k":
        w, h = (2160, 3840) if vertical else (3840, 2160)
    elif resolution == "720p":
        w, h = (720, 1280) if vertical else (1280, 720)
    else: # 1080p
        w, h = (1080, 1920) if vertical else (1920, 1080)
        
    size = f"{w}:{h}"
    
    # Visual effects list (Color grading styles)
    effects = [
        "eq=contrast=1.08:saturation=1.15,hue=h=-5:s=1.05,vignette=angle=0.12",  # Teal & Orange
        "eq=contrast=1.02:saturation=1.05,vignette=angle=0.18",                # Dreamy Glow
        "colorchannelmixer=.3:.4:.3:0:.3:.4:.3:0:.3:.4:.3,eq=contrast=1.25:brightness=-0.05,vignette=angle=0.15", # Noir
        "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131,eq=contrast=1.10:saturation=0.90,vignette=angle=0.15", # Vintage Warm
        "hue=h=15:s=1.20,eq=contrast=1.15:brightness=0.02,vignette=angle=0.10",  # Cyberpunk Neon
        "hue=h=-10:s=0.95,eq=contrast=1.05,vignette=angle=0.15",               # Cold Nordic
        "eq=contrast=1.12:saturation=1.12,vignette=angle=0.14"                  # Classic Cinematic
    ]
    effect = effects[(scene_index - 1) % len(effects)]
    
    fade_out = max(duration - 0.15, 0)
    polish = f"{effect},fade=t=in:st=0:d=0.15,fade=t=out:st={fade_out:.2f}:d=0.15"
    
    # Burn stylized uppercase caption in center
    if caption:
        safe_text = str(caption).upper().replace("'", "'\\''").replace(":", "\\:")
        safe_text = re.sub(r"[^\w\s\-\!\?\,\.\'\(\)\[\]]", "", safe_text).strip()
        
        if safe_text:
            if vertical:
                font_size = int(w * 0.065)
                box_border = int(w * 0.03)
            else:
                font_size = int(h * 0.065)
                box_border = int(h * 0.03)
                
            font_filter_part = ""
            if sys.platform == "win32":
                win_font = Path("C:/Windows/Fonts/arialbd.ttf")
                if win_font.exists():
                    font_path = str(win_font).replace("\\", "/").replace(":", "\\:")
                    font_filter_part = f"fontfile='{font_path}':"
            else:
                linux_fonts = [
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
                ]
                found = False
                for lf in linux_fonts:
                    if Path(lf).exists():
                        font_filter_part = f"fontfile='{lf}':"
                        found = True
                        break
                if not found:
                    font_filter_part = ""
                    
            drawtext_filter = (
                f"drawtext={font_filter_part}text='{safe_text}':fontcolor=white:fontsize={font_size}:"
                f"box=1:boxcolor=black@0.65:boxborderw={box_border}:"
                f"x=(w-text_w)/2:y=(h-text_h)/2"
            )
            polish = f"{polish},{drawtext_filter}"
            
    vf = f"scale={size}:force_original_aspect_ratio=increase,crop={size},setsar=1,{polish}"
    
    if media.suffix.lower() == ".mp4":
        subprocess.run([
            ffmpeg, "-y", "-stream_loop", "-1", "-i", str(media), "-i", str(audio),
            "-t", f"{duration:.2f}", 
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", 
            "-c:a", "aac", "-filter:a", "volume=3.0", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            "-b:v", "4000k" if vertical else "6000k", "-pix_fmt", "yuv420p", "-r", "30", str(out)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
    else:
        frames = int(duration * 30)
        zoom_size = f"{w}x{h}"
        
        # Randomize zoom direction/pan for dynamic image effects
        if scene_index % 4 == 0:
            zoom = f"zoompan=z='min(zoom+0.0008,1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        elif scene_index % 4 == 1:
            zoom = f"zoompan=z='max(1.06-0.0008*on,1.0)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        elif scene_index % 4 == 2:
            zoom = f"zoompan=z=1.06:x='0.001*on*iw':y='ih/2-(ih/zoom/2)'"
        else:
            zoom = f"zoompan=z=1.06:x='iw-(iw/zoom)-0.001*on*iw':y='ih/2-(ih/zoom/2)'"
            
        zoom_filter = f"{zoom}:d={frames}:s={zoom_size}:fps=30"
        image_vf = f"scale={size}:force_original_aspect_ratio=increase,crop={size},setsar=1,{zoom_filter},{polish}"
        subprocess.run([
            ffmpeg, "-y", "-loop", "1", "-i", str(media), "-i", str(audio),
            "-t", f"{duration:.2f}", 
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", image_vf, "-c:v", "libx264", "-preset", "ultrafast", 
            "-tune", "stillimage", "-c:a", "aac", "-filter:a", "volume=3.0", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            "-b:v", "4000k" if vertical else "6000k", "-pix_fmt", "yuv420p", "-r", "30", str(out)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)


def concat_wavs_with_padding(audio_items, output_path):
    sample_rate = 44100
    channels = 2
    bytes_per_frame = 4
    
    pcm_data = bytearray()
    
    for path, actual_dur in audio_items:
        wav_path = str(path)
        temp_wav = None
        need_resample = False
        try:
            with wave.open(wav_path, "rb") as wav:
                if wav.getframerate() != sample_rate or wav.getnchannels() != channels or wav.getsampwidth() != 2:
                    need_resample = True
        except Exception:
            need_resample = True
            
        if need_resample:
            import tempfile
            from pathlib import Path
            import uuid
            temp_dir = Path(tempfile.gettempdir())
            temp_file = temp_dir / f"resampled_{uuid.uuid4().hex}.wav"
            ffmpeg = ffmpeg_bin()
            try:
                print(f"Resampling {path.name} to 44100Hz Stereo PCM...")
                subprocess.run([
                    ffmpeg, "-y", "-i", wav_path,
                    "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
                    str(temp_file)
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
                wav_path = str(temp_file)
                temp_wav = temp_file
            except Exception as e:
                print(f"Failed to resample {path.name}: {e}")

        try:
            with wave.open(wav_path, "rb") as wav:
                assert wav.getframerate() == sample_rate
                assert wav.getnchannels() == channels
                assert wav.getsampwidth() == 2
                
                frames = wav.readframes(wav.getnframes())
                pcm_data.extend(frames)
                
                # Pad with silence up to actual_dur
                voice_dur = len(frames) / (sample_rate * bytes_per_frame)
                silence_dur = max(0.0, actual_dur - voice_dur)
                silence_frames = int(silence_dur * sample_rate)
                pcm_data.extend(b"\x00" * (silence_frames * bytes_per_frame))
        finally:
            if temp_wav and temp_wav.exists():
                try:
                    temp_wav.unlink()
                except Exception:
                    pass
            
    with wave.open(str(output_path), "wb") as out_wav:
        out_wav.setnchannels(channels)
        out_wav.setsampwidth(2)
        out_wav.setframerate(sample_rate)
        out_wav.writeframes(bytes(pcm_data))


def render_final(ffmpeg, scene_files, srt, output, vertical, master_wav_path=None, music_path=None, resolution="1080p"):
    concat_file = output.parent / "concat.txt"
    concat_rows = []
    for path in scene_files:
        try:
            rel_path = os.path.relpath(path, concat_file.parent).replace("\\", "/")
            concat_rows.append(f"file '{rel_path}'")
        except Exception:
            safe_path = path.resolve().as_posix().replace("'", "'\\''")
            concat_rows.append(f"file '{safe_path}'")
            
    concat_file.write_text("\n".join(concat_rows), encoding="utf-8")
    raw = output.with_name(output.stem + "_raw.mp4")
    
    raw_video_with_audio = output.with_name(output.stem + "_raw_audio.mp4")
    concat_success = False
    
    if durations and len(scene_files) > 1:
        try:
            concat_clips_with_transitions(ffmpeg, scene_files, durations, raw_video_with_audio)
            concat_success = True
        except Exception as e:
            print(f"Transition concat failed: {e}. Falling back to standard concat.")
            import traceback
            traceback.print_exc()
            
    if not concat_success:
        if master_wav_path and master_wav_path.exists():
            raw_video = output.with_name(output.stem + "_video.mp4")
            subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-an", "-c:v", "copy", str(raw_video)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
            if music_path and music_path.exists():
                subprocess.run([
                    ffmpeg, "-y",
                    "-i", str(raw_video),
                    "-i", str(master_wav_path),
                    "-stream_loop", "-1", "-i", str(music_path),
                    "-filter_complex", "[1:a]volume=3.0[voice_raw]; [voice_raw]asplit=2[voice1][voice2]; [2:a]volume=0.35[music_raw]; [music_raw][voice1]sidechaincompress=threshold=0.15:ratio=4.5:attack=100:release=600[music_ducked]; [voice2][music_ducked]amix=inputs=2:duration=first[a]",
                    "-map", "0:v:0", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    str(raw)
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
            else:
                subprocess.run([ffmpeg, "-y", "-i", str(raw_video), "-i", str(master_wav_path), "-c:v", "copy", "-c:a", "aac", "-filter:a", "volume=3.0", "-b:a", "192k", str(raw)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
            if raw_video.exists():
                try: raw_video.unlink()
                except Exception: pass
        else:
            if music_path and music_path.exists():
                subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(raw_video_with_audio)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
                subprocess.run([
                    ffmpeg, "-y",
                    "-i", str(raw_video_with_audio),
                    "-stream_loop", "-1", "-i", str(music_path),
                    "-filter_complex", "[0:a]volume=1.0[voice_raw]; [voice_raw]asplit=2[voice1][voice2]; [1:a]volume=0.25[music_raw]; [music_raw][voice1]sidechaincompress=threshold=0.15:ratio=4.5:attack=100:release=600[music_ducked]; [voice2][music_ducked]amix=inputs=2:duration=first[a]",
                    "-map", "0:v:0", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    str(raw)
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
                if raw_video_with_audio.exists():
                    try: raw_video_with_audio.unlink()
                    except Exception: pass
            else:
                subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(raw)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
    else:
        if music_path and music_path.exists():
            subprocess.run([
                ffmpeg, "-y",
                "-i", str(raw_video_with_audio),
                "-stream_loop", "-1", "-i", str(music_path),
                "-filter_complex", "[0:a]volume=1.0[voice_raw]; [voice_raw]asplit=2[voice1][voice2]; [1:a]volume=0.25[music_raw]; [music_raw][voice1]sidechaincompress=threshold=0.15:ratio=4.5:attack=100:release=600[music_ducked]; [voice2][music_ducked]amix=inputs=2:duration=first[a]",
                "-map", "0:v:0", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                str(raw)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
            if raw_video_with_audio.exists():
                try: raw_video_with_audio.unlink()
                except Exception: pass
        else:
            shutil.copy2(raw_video_with_audio, raw)
            if raw_video_with_audio.exists():
                try: raw_video_with_audio.unlink()
                except Exception: pass
    
    # Conditionally burn subtitles only for English language voices
    # (i.e. if the voice doesn't start with 'ur-' or 'hi-')
    is_english = True
    if srt:
        srt_name = srt.name.lower()
        job_dir_name = srt.parent.name
        job_id = job_dir_name
        active_voice = ""
        if job_id in jobs:
            active_voice = jobs[job_id].get("voice", "")
        elif job_id.replace("runs_", "") in jobs:
            active_voice = jobs[job_id.replace("runs_", "")].get("voice", "")
            
        if active_voice:
            lang = active_voice.split("-")[0].lower()
            if lang in ["ur", "hi"]:
                is_english = False
 
    if is_english and srt and srt.exists() and srt.stat().st_size > 0:
        srt_path = str(srt).replace("\\", "/").replace(":", "\\:")
        # Premium subtitles scaled according to resolution
        if resolution == "4k":
            font_size = 76 if vertical else 48
            margin = 160 if vertical else 90
        elif resolution == "720p":
            font_size = 25 if vertical else 16
            margin = 53 if vertical else 30
        else: # 1080p
            font_size = 38 if vertical else 24
            margin = 80 if vertical else 45
            
        vf = f"subtitles='{srt_path}':force_style='FontName=Arial,FontSize={font_size},Italic=0,Bold=0,PrimaryColour=&H00FFFFFF,BorderStyle=3,Outline=3,Shadow=1,OutlineColour=&H6098593B,ShadowColour=&H80000000,Alignment=2,MarginV={margin}'"
        subprocess.run([ffmpeg, "-y", "-i", str(raw), "-vf", vf, "-preset", "ultrafast", "-c:a", "copy", str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
    else:
        # Just copy video directly to output without burning subtitles
        subprocess.run([ffmpeg, "-y", "-i", str(raw), "-c:v", "copy", "-c:a", "copy", str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)


def create_fallback_image(dest, vertical, resolution="1080p"):
    if resolution == "4k":
        size = "2160x3840" if vertical else "3840x2160"
    elif resolution == "720p":
        size = "720x1280" if vertical else "1280x720"
    else:
        size = "1080x1920" if vertical else "1920x1080"
        
    try:
        subprocess.run([
            ffmpeg_bin(), "-y", "-f", "lavfi", "-i", f"color=c=black:s={size}", "-frames:v", "1", str(dest)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
    except Exception as e:
        print(f"Failed to create fallback image: {e}")


def process_scene_assets(i, scene, job_dir, pexels_key, vertical, voice, used_media_urls, asset_type="mix", rate="+3%", topic="", resolution="1080p", use_custom_vo=False, local_folder_path=""):
    image = job_dir / f"scene_{i:03}.jpg"
    video = job_dir / f"scene_{i:03}.mp4"
    audio = job_dir / f"scene_{i:03}.wav"

    # Smart Cache / Resume check with validation
    txt_path = job_dir / f"scene_{i:03}.txt"
    visual_path = job_dir / f"scene_{i:03}.visual"
    
    # If the actual asset files already exist, but the metadata validation files
    # do NOT exist yet (e.g., from a restored old run), we assume the cache is valid
    # to protect and reuse their previously completed renders!
    audio_exists = audio.exists() and audio.stat().st_size > 0 and wav_duration(audio) > 0.0
    video_exists = video.exists() and video.stat().st_size > 0
    image_exists = image.exists() and image.stat().st_size > 0
    assets_exist = audio_exists and (video_exists or image_exists)
    
    cache_valid = False
    if assets_exist and not (txt_path.exists() or visual_path.exists()):
        cache_valid = True
    elif txt_path.exists() and visual_path.exists():
        try:
            cached_text = txt_path.read_text(encoding="utf-8").strip()
            cached_visual = visual_path.read_text(encoding="utf-8").strip()
            if cached_text == str(scene.get("narration", "")).strip() and cached_visual == str(scene.get("visual", "")).strip():
                cache_valid = True
        except Exception:
            pass

    if not cache_valid:
        # Invalidate old assets
        for suffix in [".jpg", ".mp4", ".wav", ".txt", ".visual"]:
            try:
                (job_dir / f"scene_{i:03}{suffix}").unlink()
            except Exception:
                pass
        # Delete compiled clip too
        clip_path = job_dir / f"clip_{i:03}.mp4"
        try:
            clip_path.unlink()
        except Exception:
            pass

    audio_exists = audio.exists() and audio.stat().st_size > 0 and wav_duration(audio) > 0.0
    video_exists = video.exists() and video.stat().st_size > 0
    image_exists = image.exists() and image.stat().st_size > 0

    if audio_exists and (video_exists or image_exists):
        print(f"Scene {i} assets already exist. Reusing.")
        return i, (video if video_exists else image), audio

    # Determine what type of asset we want based on asset_type config
    prefer_image = False
    if asset_type == "ai_images":
        prefer_image = True
    elif asset_type == "mix":
        # Alternate between videos (odd scenes) and AI images (even scenes)
        prefer_image = (i % 2 == 0)

    if prefer_image:
        media = image
        try:
            if not image_exists:
                print(f"Generating AI image for scene {i} using Pollinations...")
                download_pollinations_image(scene["visual"], image, vertical, topic)
            if not image.exists() or image.stat().st_size == 0:
                print(f"Pollinations failed for scene {i}, trying Pexels image fallback...")
                download_pexels_image(scene["visual"], image, pexels_key, vertical, used_media_urls, topic)
            if not image.exists() or image.stat().st_size == 0:
                print(f"Using fallback black image for scene {i}")
                create_fallback_image(image, vertical, resolution=resolution)
        except Exception as exc:
            print(f"Error downloading AI image for scene {i}: {exc}")
            if not image.exists():
                create_fallback_image(image, vertical, resolution=resolution)
    else:
        # Standard video preference (stock_videos or odd scenes in mix)
        media = video
        try:
            if not video_exists:
                if asset_type == "local_videos":
                    local_dir = Path(local_folder_path)
                    video_file = None
                    if local_dir.exists() and local_dir.is_dir():
                        # Look for exact match first (e.g. 1.mp4, 2.mp4)
                        exact_match = local_dir / f"{i}.mp4"
                        if exact_match.exists():
                            video_file = exact_match
                        else:
                            # Fallback: get all mp4 files and sort them numerically
                            all_mp4s = list(local_dir.glob("*.mp4"))
                            if all_mp4s:
                                def get_num(p):
                                    nums = re.findall(r'\d+', p.stem)
                                    return int(nums[0]) if nums else 999999
                                all_mp4s.sort(key=lambda p: (get_num(p), p.name.lower()))
                                video_file = all_mp4s[(i - 1) % len(all_mp4s)]
                    if video_file and video_file.exists():
                        shutil.copy2(video_file, video)
                        print(f"Copied local video for scene {i} from {video_file}")
                    else:
                        print(f"Local video for scene {i} not found in {local_dir}! Falling back to stock video.")
                        download_pexels_video(scene["visual"], video, pexels_key, vertical, used_media_urls, topic)
                else:
                    custom_url = scene.get("customVideoUrl")
                    if custom_url:
                        print(f"Downloading custom video from: {custom_url}")
                        res = requests.get(custom_url, stream=True, timeout=60)
                        res.raise_for_status()
                        with open(video, "wb") as f:
                            for chunk in res.iter_content(chunk_size=8192):
                                f.write(chunk)
                    else:
                        download_pexels_video(scene["visual"], video, pexels_key, vertical, used_media_urls, topic)
            if not video.exists() or video.stat().st_size == 0:
                media = image
                if not image.exists():
                    downloaded = False
                    if download_pexels_image(scene["visual"], image, pexels_key, vertical, used_media_urls, topic):
                        downloaded = True
                    else:
                        try:
                            print(f"Pexels video/image failed for scene {i}, trying Pollinations AI image...")
                            download_pollinations_image(scene["visual"], image, vertical, topic)
                            downloaded = True
                        except Exception as e:
                            print(f"Pollinations download failed for scene {i}: {e}")
                    if not downloaded and not image.exists():
                        print(f"Using fallback black image for scene {i}")
                        create_fallback_image(image, vertical, resolution=resolution)
        except Exception as exc:
            print(f"Error downloading media for scene {i}: {exc}")
            if not video.exists() and not image.exists():
                media = image
                create_fallback_image(image, vertical, resolution=resolution)

    try:
        if not audio.exists() or audio.stat().st_size == 0:
            if use_custom_vo:
                # Generate silent audio of the exact scene duration
                dur = float(scene.get("duration", 4))
                write_silent_wav(dur, audio)
                print(f"Generated silent audio for scene {i} of duration {dur}s")
            else:
                custom_audio = scene.get("customAudio")
                if custom_audio:
                    src_path = RUNS / custom_audio
                    if src_path.exists():
                        shutil.copy(src_path, audio)
                        print(f"Copied custom audio for scene {i} from {custom_audio}")
                    else:
                        print(f"Custom audio path {src_path} not found! Falling back to TTS.")
                        write_tts_wav(scene["narration"], audio, voice, rate)
                else:
                    write_tts_wav(scene["narration"], audio, voice, rate)
    except Exception as exc:
        print(f"Error generating voice for scene {i}: {exc}")

    # --- GUARANTEED FALLBACK (crash fix) ---
    # No matter what happened above (edge-tts blocked on cloud IP, Google TTS
    # blocked/rate-limited, or any other silent exception), make 100% sure a
    # valid audio file exists before this function returns. Without this,
    # wav_duration() downstream crashes with FileNotFoundError when a scene's
    # TTS silently failed on cloud hosts like Render (this was the root cause
    # of the 45% crash on the deployed backend).
    if not audio.exists() or audio.stat().st_size == 0:
        try:
            fallback_dur = float(scene.get("duration", 4)) if use_custom_vo else 4.0
            write_silent_wav(fallback_dur, audio)
            print(f"[FIX] Forced silent-audio fallback for scene {i} — all TTS methods failed on this server.")
        except Exception as final_exc:
            print(f"[FIX] CRITICAL: even silent-wav fallback failed for scene {i}: {final_exc}")
            # last resort: write minimal silence directly, should never fail
            import wave as _wave
            with _wave.open(str(audio), "wb") as _w:
                _w.setnchannels(2); _w.setsampwidth(2); _w.setframerate(44100)
                _w.writeframes(b"\x00" * (44100 * 4 * 4))

    # Write cache validation files to lock in the successfully verified state
    try:
        txt_path.write_text(str(scene.get("narration", "")).strip(), encoding="utf-8")
        visual_path.write_text(str(scene.get("visual", "")).strip(), encoding="utf-8")
    except Exception:
        pass
        
    return i, media, audio


def run_job(job_id, payload):
    try:
        topic = payload.get("title", "").strip()
        style = payload.get("style", "documentary")
        format_name = payload.get("format", "youtube")
        vertical = format_name == "short"
        duration_type = payload.get("duration", "1m")
        asset_type = payload.get("assetType", "mix")
        resolution = payload.get("resolution", "1080p")
        
        gemini_key = env_or(payload.get("geminiKey"), "GEMINI_API_KEY")
        pexels_key = env_or(payload.get("pexelsKey"), "PEXELS_API_KEY") or DEFAULT_PEXELS_API_KEY
        voice = payload.get("voice") or "en-US-AriaNeural"
        local_folder_path = payload.get("localFolderPath", "").strip()
        
        # Determine voice rate based on payload parameter or style for professional storytelling pacing
        voice_rate = payload.get("voiceRate")
        if not voice_rate:
            if style == "story":
                voice_rate = "-12%"
            elif style == "documentary":
                voice_rate = "-6%"
            else:
                voice_rate = "+0%"
            
        # Store active voice code in job object so subtitles conditional check can read it
        jobs[job_id] = {
            "progress": 0,
            "status": "running",
            "voice": voice,
            "createdAt": time.time(),
            "updatedAt": time.time()
        }
            
        job_dir = RUNS / job_id
        job_dir.mkdir(exist_ok=True)
        # Save payload for resume functionality
        try:
            (job_dir / "payload.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        
        draft_id = payload.get("script", {}).get("draftId")
        custom_vo_path = RUNS / f"draft_{draft_id}" / "custom_voiceover.wav" if draft_id else None
        use_custom_vo = False
        if custom_vo_path and custom_vo_path.exists():
            use_custom_vo = True
            shutil.copy2(custom_vo_path, job_dir / "master.wav")
            print("Using custom master voiceover.")
            
        ffmpeg = ffmpeg_bin()

        music_choice = payload.get("musicTrack", "cinematic")
        music_path = None
        if music_choice and music_choice != "none":
            music_url = MUSIC_TRACKS.get(music_choice, MUSIC_TRACKS["cinematic"])
            music_dir = ASSETS / "music"
            music_dir.mkdir(exist_ok=True, parents=True)
            music_filename = music_url.split("/")[-1]
            cached_music_path = music_dir / music_filename
            if not cached_music_path.exists() or cached_music_path.stat().st_size == 0:
                print(f"Downloading background music: {music_url}")
                try:
                    res = requests.get(music_url, timeout=30)
                    res.raise_for_status()
                    cached_music_path.write_bytes(res.content)
                except Exception as e:
                    print(f"Failed to download background music: {e}")
                    cached_music_path = None
            if cached_music_path and cached_music_path.exists():
                music_path = cached_music_path

        if payload.get("script", {}).get("isLongForm"):
            # LONG-FORM PIPELINE (Chapter-based)
            outline = payload["script"]
            num_chapters = len(outline["chapters"])
            
            # Read language from selected voice
            language = "English"
            if voice.startswith("ur-"):
                language = "Urdu"
            elif voice.startswith("hi-"):
                language = "Hindi"
            
            duration_map = {
                "5m": (5.0, 15),
                "10m": (10.0, 15),
                "30m": (30.0, 15),
                "1h": (60.0, 15),
                "2h": (120.0, 15)
            }
            duration_mins, scenes_per_chapter = duration_map.get(outline.get("duration", "5m"), (5.0, 15))
            
            chapter_files = []
            all_subtitles = []
            subtitle_index = 1
            global_cursor = 0.0
            long_form_audio_items = []
            all_durations = []
            scene_idx_global = 1
            
            full_script = {
                "title": outline["title"],
                "description": f"A professional {outline.get('duration')} documentary about {topic}.",
                "keywords": [topic, style, format_name],
                "scenes": []
            }
            
            # Load root script.json cache if it exists
            root_script = None
            root_script_path = job_dir / "script.json"
            if root_script_path.exists():
                try:
                    root_script = json.loads(root_script_path.read_text(encoding="utf-8"))
                    print("Loaded root script.json cache.")
                except Exception:
                    pass

            full_outline_desc = "\n".join([
                f"Chapter {i}: {ch.get('title', '')} - {ch.get('description', '')}"
                for i, ch in enumerate(outline.get("chapters", []), 1)
            ])

            for ch_idx, chapter in enumerate(outline["chapters"], 1):
                if jobs.get(job_id, {}).get("status") != "running":
                    print(f"Job {job_id} paused/stopped. Aborting long-form rendering.")
                    return
                    
                prog_base = 5 + int((ch_idx - 1) / num_chapters * 80)
                
                ch_dir = job_dir / f"chapter_{ch_idx:03}"
                ch_dir.mkdir(exist_ok=True)
                
                ch_script_path = ch_dir / "script.json"
                ch_script = None
                
                # Check root script cache first
                if root_script:
                    try:
                        start_i = (ch_idx - 1) * scenes_per_chapter
                        end_i = ch_idx * scenes_per_chapter
                        ch_scenes = root_script["scenes"][start_i:end_i]
                        if ch_scenes:
                            ch_script = {
                                "title": chapter.get("title", f"Chapter {ch_idx}"),
                                "scenes": ch_scenes
                            }
                            print(f"Chapter {ch_idx} script loaded from root cache.")
                    except Exception:
                        ch_script = None
                        
                # Check chapter-level script cache second
                if not ch_script and ch_script_path.exists():
                    try:
                        ch_script = json.loads(ch_script_path.read_text(encoding="utf-8"))
                        print(f"Chapter {ch_idx} script loaded from chapter cache.")
                    except Exception:
                        ch_script = None

                # Verify that cached chapter script matches request outline title to enable custom updates
                cache_valid = False
                if ch_script:
                    cached_title = ch_script.get("title", "")
                    req_title = chapter.get("title", "")
                    if cached_title.strip().lower() == req_title.strip().lower():
                        cache_valid = True
                if not cache_valid:
                    ch_script = None
                
                if not ch_script:
                    prev_narration = ""
                    if ch_idx > 1 and len(full_script["scenes"]) > 0:
                        last_scenes = full_script["scenes"][-20:]
                        prev_narration = " ".join([s.get("narration", "") for s in last_scenes])
                        
                    update(job_id, "script", prog_base, f"Writing script for Chapter {ch_idx}/{num_chapters}: {chapter['title']}")
                    ch_script = generate_chapter_script(
                        topic, outline["title"], chapter, ch_idx, scenes_per_chapter, style, format_name, gemini_key, language,
                        prev_narration=prev_narration,
                        full_outline_desc=full_outline_desc
                    )
                    try:
                        ch_script_path.write_text(json.dumps(ch_script, indent=2, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                else:
                    update(job_id, "script", prog_base, f"Loaded cached script for Chapter {ch_idx}/{num_chapters}: {chapter['title']}")
                
                full_script["scenes"].extend(ch_script["scenes"])
                
                update(job_id, "assets", prog_base + 2, f"Downloading assets for Chapter {ch_idx}/{num_chapters}...")
                
                scenes_data = {}
                used_media_urls = load_global_used_media()
                
                ch_total = len(ch_script["scenes"])
                with ThreadPoolExecutor(max_workers=20) as executor:
                    futures = []
                    for i, scene in enumerate(ch_script["scenes"], 1):
                        futures.append(
                            executor.submit(
                                process_scene_assets, i, scene, ch_dir, pexels_key, vertical, voice, used_media_urls, asset_type, voice_rate, topic, resolution, False, local_folder_path
                            )
                        )
                    ch_completed = 0
                    for future in as_completed(futures):
                        idx, media_path, audio_path = future.result()
                        scenes_data[idx] = {"media": media_path, "audio": audio_path}
                        ch_completed += 1
                        prog = prog_base + int((ch_completed / ch_total) * 3)
                        update(job_id, "assets", prog, f"Chapter {ch_idx}: Verified scene {ch_completed}/{ch_total}")
                save_global_used_media(used_media_urls)
                
                update(job_id, "render", prog_base + 5, f"Rendering Chapter {ch_idx}/{num_chapters}...")
                
                # Calculate durations and subtitles first
                ch_durations = {}
                for i in range(1, len(ch_script["scenes"]) + 1):
                    scene = ch_script["scenes"][i - 1]
                    audio = scenes_data[i]["audio"]
                    voice_duration = wav_duration(audio)
                    if voice_duration > 4.75:
                        speed_factor = voice_duration / 4.75
                        if speed_factor <= 2.0:
                            temp_audio = audio.with_name(audio.stem + "_temp.wav")
                            try:
                                subprocess.run([
                                    ffmpeg, "-y", "-i", str(audio),
                                    "-filter:a", f"atempo={speed_factor:.3f}",
                                    str(temp_audio)
                                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
                                shutil.move(str(temp_audio), str(audio))
                                voice_duration = wav_duration(audio)
                            except Exception as e:
                                print(f"Failed to speed up audio: {e}")
                    actual_duration = max(3.0, min(5.0, voice_duration + 0.25))
                    ch_durations[i] = actual_duration
                    all_durations.append(actual_duration)
                    
                    adjusted_cursor = global_cursor - (scene_idx_global - 1) * 0.4
                    rows, subtitle_index = split_subtitles(
                        scene["narration"], adjusted_cursor, voice_duration, subtitle_index
                    )
                    all_subtitles.extend(rows)
                    global_cursor += actual_duration
                    long_form_audio_items.append((audio, actual_duration))
                    scene_idx_global += 1
                
                # Render clips in parallel
                def ch_render_worker(i):
                    if jobs.get(job_id, {}).get("status") != "running":
                        raise RuntimeError("Job paused/stopped by user")
                    scene = ch_script["scenes"][i - 1]
                    clip = ch_dir / f"clip_{i:03}.mp4"
                    # Smart Resume Check for scene clip
                    if clip.exists() and clip.stat().st_size > 50000:
                        print(f"Clip {clip.name} already exists. Skipping render.")
                        return clip
                    media = scenes_data[i]["media"]
                    audio = scenes_data[i]["audio"]
                    actual_duration = ch_durations[i]
                    caption = str(scene.get("caption") or "").strip().upper()
                    render_scene(ffmpeg, media, audio, clip, vertical, actual_duration, resolution=resolution, caption=caption, scene_index=i)
                    return clip
                
                completed_ch_render = 0
                ch_total = len(ch_script["scenes"])
                clips = [None] * ch_total
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {executor.submit(ch_render_worker, i): i for i in range(1, ch_total + 1)}
                    for future in as_completed(futures):
                        i = futures[future]
                        clip = future.result()
                        clips[i - 1] = clip
                        completed_ch_render += 1
                        total_scenes_approx = num_chapters * ch_total
                        current_scene_global = (ch_idx - 1) * ch_total + completed_ch_render
                        prog = 45 + int((current_scene_global / total_scenes_approx) * 40)
                        update(job_id, "render", prog, f"Rendered scene {completed_ch_render}/{ch_total} in chapter {ch_idx}")
                chapter_files.extend(clips)
            
            # Check if job was paused/stopped
            if jobs.get(job_id, {}).get("status") != "running":
                print(f"Job {job_id} paused/stopped. Aborting long-form final render.")
                return
                
            update(job_id, "render", 90, "Assembling all chapters and burning global subtitles...")
            srt = job_dir / "subtitles.srt"
            srt.write_text("\n".join(all_subtitles), encoding="utf-8")
            
            (job_dir / "script.json").write_text(json.dumps(full_script, indent=2, ensure_ascii=False), encoding="utf-8")
            jobs[job_id]["script"] = full_script
            
            safe = slugify(full_script["title"])
            final = OUTPUT / f"{safe}-{job_id[:8]}.mp4"
            master_wav = job_dir / "master.wav"
            concat_wavs_with_padding(long_form_audio_items, master_wav)
            render_final(ffmpeg, chapter_files, srt, final, vertical, master_wav, music_path, resolution=resolution, durations=all_durations)
            
        else:
            # SHORT-FORM PIPELINE (15s, 30s, 1m)
            if "script" in payload:
                script = payload["script"]
                update(job_id, "script", 10, "Using custom edited script")
            else:
                cached_script_path = job_dir / "script.json"
                script = None
                if cached_script_path.exists():
                    try:
                        script = json.loads(cached_script_path.read_text(encoding="utf-8"))
                        update(job_id, "script", 10, "Resuming from cached script")
                    except Exception:
                        script = None
                
                if not script:
                    update(job_id, "script", 8, "Researching topic and writing script")
                    scene_map = {"15s": 4, "30s": 8, "1m": 15}
                    scene_count = scene_map.get(duration_type, 15)
                    script = generate_script(topic, style, format_name, scene_count, gemini_key, language)
                
            (job_dir / "script.json").write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
            jobs[job_id]["script"] = script
            
            update(job_id, "assets", 15, "Downloading media assets and generating voiceovers...")
            scenes_data = {}
            used_media_urls = load_global_used_media()
            
            total = len(script["scenes"])
            with ThreadPoolExecutor(max_workers=20) as executor:
                futures = []
                for i, scene in enumerate(script["scenes"], 1):
                    futures.append(
                        executor.submit(
                            process_scene_assets, i, scene, job_dir, pexels_key, vertical, voice, used_media_urls, asset_type, voice_rate, topic, resolution, use_custom_vo, local_folder_path
                        )
                    )
                completed_count = 0
                for future in as_completed(futures):
                    idx, media_path, audio_path = future.result()
                    scenes_data[idx] = {"media": media_path, "audio": audio_path}
                    completed_count += 1
                    prog = 15 + int((completed_count / total) * 30)
                    update(job_id, "assets", prog, f"Verified assets for scene {completed_count}/{total}")
            save_global_used_media(used_media_urls)
            
            scene_files = []
            subtitles = []
            subtitle_index = 1
            cursor = 0.0
            total = len(script["scenes"])
            
            # Calculate durations and subtitles first
            scene_durations = {}
            short_form_audio_items = []
            durations_list = []
            for i in range(1, total + 1):
                scene = script["scenes"][i - 1]
                audio = scenes_data[i]["audio"]
                if use_custom_vo:
                    actual_duration = max(3.0, min(5.0, float(scene.get("duration", 4.0))))
                    voice_duration = actual_duration
                else:
                    voice_duration = wav_duration(audio)
                    if voice_duration > 4.75:
                        speed_factor = voice_duration / 4.75
                        if speed_factor <= 2.0:
                            temp_audio = audio.with_name(audio.stem + "_temp.wav")
                            try:
                                subprocess.run([
                                    ffmpeg, "-y", "-i", str(audio),
                                    "-filter:a", f"atempo={speed_factor:.3f}",
                                    str(temp_audio)
                                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, creationflags=SUBPROCESS_FLAGS)
                                shutil.move(str(temp_audio), str(audio))
                                voice_duration = wav_duration(audio)
                            except Exception as e:
                                print(f"Failed to speed up audio: {e}")
                    actual_duration = max(3.0, min(5.0, voice_duration + 0.25))
                scene_durations[i] = actual_duration
                durations_list.append(actual_duration)
                
                adjusted_cursor = cursor - (i - 1) * 0.4
                rows, subtitle_index = split_subtitles(scene["narration"], adjusted_cursor, voice_duration, subtitle_index)
                subtitles.extend(rows)
                cursor += actual_duration
                short_form_audio_items.append((audio, actual_duration))
                
            # Render all clips in parallel
            update(job_id, "render", 45, "Rendering scenes in parallel...")
            
            def render_worker(i):
                if jobs.get(job_id, {}).get("status") != "running":
                    raise RuntimeError("Job paused/stopped by user")
                scene = script["scenes"][i - 1]
                clip = job_dir / f"clip_{i:03}.mp4"
                # Smart Resume Check for scene clip
                if clip.exists() and clip.stat().st_size > 50000:
                    print(f"Clip {clip.name} already exists. Skipping render.")
                    return clip
                media = scenes_data[i]["media"]
                audio = scenes_data[i]["audio"]
                actual_duration = scene_durations[i]
                caption = str(scene.get("caption") or "").strip().upper()
                render_scene(ffmpeg, media, audio, clip, vertical, actual_duration, resolution=resolution, caption=caption, scene_index=i)
                return clip
                
            completed_render = 0
            scene_files = [None] * total
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(render_worker, i): i for i in range(1, total + 1)}
                for future in as_completed(futures):
                    i = futures[future]
                    clip = future.result()
                    scene_files[i - 1] = clip
                    completed_render += 1
                    prog = 45 + int((completed_render / total) * 40)
                    update(job_id, "render", prog, f"Rendered scene {completed_render}/{total}")
            
            # Check if job was paused/stopped
            if jobs.get(job_id, {}).get("status") != "running":
                print(f"Job {job_id} paused/stopped. Aborting short-form final render.")
                return
                
            srt = job_dir / "subtitles.srt"
            srt.write_text("\n".join(subtitles), encoding="utf-8")
            
            safe = slugify(script["title"])
            final = OUTPUT / f"{safe}-{job_id[:8]}.mp4"
            master_wav = job_dir / "master.wav"
            if not use_custom_vo:
                concat_wavs_with_padding(short_form_audio_items, master_wav)
            update(job_id, "render", 88, "Editing scenes, burning subtitles, and exporting MP4")
            render_final(ffmpeg, scene_files, srt, final, vertical, master_wav, music_path, resolution=resolution, durations=durations_list)
            
        update(job_id, "done", 100, "Video ready")
        jobs[job_id]["status"] = "done"
        jobs[job_id]["downloadUrl"] = f"/download/{final.name}"
        jobs[job_id]["fileName"] = final.name
        jobs[job_id]["filePath"] = str(final)
        jobs[job_id]["file"] = str(final)
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print("ERROR IN RUN_JOB:")
        print(tb)
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = tb
        update(job_id, "error", jobs[job_id].get("progress", 0), tb)


@app.get("/")
def home():
    return send_from_directory(ROOT, "agent.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/code-check")
def code_check():
    try:
        content = Path(__file__).read_text(encoding="utf-8")
        has_map = "executor.map(render_worker" in content
        return jsonify({
            "has_old_executor_map": has_map,
            "file_size": len(content),
            "platform": sys.platform
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.get("/api/latest")
def latest():
    files = sorted(OUTPUT.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    files = [p for p in files if not p.stem.endswith("_raw")]
    if not files:
        return jsonify({"file": None})
    file = files[0]
    return jsonify({
        "fileName": file.name,
        "filePath": str(file),
        "downloadUrl": f"/download/{file.name}",
        "updatedAt": file.stat().st_mtime,
        "size": file.stat().st_size,
    })


@app.get("/api/history")
def get_history():
    if not is_license_activated():
        return jsonify({"error": "Key is not valid"}), 403
    files = sorted(OUTPUT.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    files = [p for p in files if not p.name.endswith("_raw.mp4")]
    history = []
    for file in files:
        history.append({
            "fileName": file.name,
            "downloadUrl": f"/download/{file.name}",
            "size": file.stat().st_size,
            "createdAt": file.stat().st_mtime
        })
    return jsonify(history)


@app.post("/api/open-output")
def open_output():
    if not is_license_activated():
        return jsonify({"error": "Key is not valid"}), 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("fileName") or "")
    target = OUTPUT / name if name else OUTPUT
    if name and target.exists():
        subprocess.Popen(["explorer", "/select,", str(target)], creationflags=SUBPROCESS_FLAGS)
    else:
        subprocess.Popen(["explorer", str(OUTPUT)], creationflags=SUBPROCESS_FLAGS)
    return jsonify({"ok": True})


@app.route("/api/pexels-search")
def api_pexels_search():
    if not is_license_activated():
        return jsonify({"error": "Key is not valid"}), 403
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify([])
    headers = {"Authorization": DEFAULT_PEXELS_API_KEY}
    try:
        res = requests.get(
            "https://api.pexels.com/videos/search",
            params={"query": query, "per_page": 8},
            headers=headers,
            timeout=10
        )
        if res.ok:
            data = res.json()
            videos = []
            for v in data.get("videos", []):
                # Get suitable video file link
                files = v.get("video_files", [])
                files = sorted(files, key=lambda f: abs((f.get("width") or 0) - 1280))
                if files:
                    videos.append({
                        "id": v["id"],
                        "url": files[0]["link"],
                        "image": v["image"],
                        "duration": v["duration"]
                    })
            return jsonify(videos)
    except Exception as e:
        print("Pexels search failed:", e)
    return jsonify([])


def call_gemini_raw_prompt(prompt, api_key):
    models = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
    last_error = ""
    for model in models:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            res = requests.post(
                url,
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.75,
                        "maxOutputTokens": 8192,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=60,
            )
            if not res.ok:
                print(f"[Gemini Error] Model {model} failed with status {res.status_code}: {res.text}")
                last_error = res.text[:300]
                continue
            raw = res.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"```json|```", "", raw).strip())
        except Exception as exc:
            print(f"[Gemini Exception] Model {model} threw exception: {exc}")
            last_error = str(exc)
    raise RuntimeError("Gemini content generation failed: " + last_error)

def segment_custom_script(topic, custom_script, style, format_name, scene_count, api_key, language="English"):
    prompt = f"""
You are a premium documentary video editor and AI script director.
I have a custom script narration. Your task is to segment this exact script into exactly {scene_count} chronological scenes.

CRITICAL INSTRUCTIONS:
1. Do NOT modify, rewrite, summarize, or change a single word of the input custom script narration. You must distribute the exact script text chronologically across the {scene_count} scenes. Each scene narration MUST be short (approx 6 to 10 words, max 12 words) so that it can be spoken in 3 to 5 seconds.
2. The narration property of the scenes, when joined together sequentially, MUST reconstruct the original custom script exactly (whitespace differences are ok, but words must match 100%).
3. Create visual search queries and captions for each scene.

Return ONLY valid JSON matching this schema:
{{
  "title": "Poetic documentary title based on the script",
  "description": "Engaging description",
  "keywords": ["keyword1", "keyword2"],
  "scenes": [
    {{
      "title": "short scene title",
      "narration": "the exact section of the custom script for this scene",
      "caption": "short punchy dramatic caption (2-4 words)",
      "visual": "cinematic visual search query (ALWAYS in English)",
      "duration": 4
    }}
  ]
}}

VISUAL SEARCH QUERY DIRECTIVES:
- If the scene discusses a country, geographical region, or border, specify a map prompt. e.g. "3D animated satellite map of India with glowing borders, drone flyover cartography graphic style", "vintage historical map of Europe panning shot".
- If the scene discusses numbers, stats, or data, specify a data prompt. e.g. "holographic stock market financial data chart glowing animation", "futuristic tech world map connections database graphics".
- For other scenes, use high-quality physical descriptors (e.g. 'cinematic lighting, slow motion, epic panning shot').

Input Custom Script:
"{custom_script}"
"""
    return call_gemini_raw_prompt(prompt, api_key)

def get_visual_query(narration, topic):
    import re
    text = narration.lower()
    
    # 1. Map/Location detection for geographical contexts
    if any(w in text for w in ["map", "located", "geography", "border", "country", "territory", "continent", "region"]):
        countries = ["afghanistan", "pakistan", "yemen", "saudi arabia", "america", "usa", "iraq", "syria", "egypt", "rome", "italy", "china", "russia", "england", "london", "europe", "germany", "vietnam", "turkey", "iran", "india", "japan"]
        found_country = None
        for c in countries:
            if c in text:
                found_country = c
                break
        if found_country:
            return f"map of {found_country.title()}"
        return "world map graphic"
        
    # 2. Financial / Data charts
    if any(w in text for w in ["percent", "%", "stat", "number", "growth", "money", "billion", "million", "dollar", "wealth", "economy", "stock"]):
        return "stock market chart data"
        
    # 3. War / Soldiers / Military
    if any(w in text for w in ["war", "battle", "soldier", "military", "combat", "fight", "army", "attack", "force", "weapon", "gun"]):
        return "military soldier combat"
        
    # 4. Helicopters / Aircraft / Flight
    if any(w in text for w in ["helicopter", "plane", "aircraft", "jet", "flying", "air force", "sky"]):
        return "helicopter flying"
        
    # 5. Buildings / Compounds / Mansions
    if any(w in text for w in ["compound", "house", "mansion", "palace", "building", "estate", "room", "office"]):
        return "mansion compound exterior"
        
    # 6. Desert / Terrain
    if any(w in text for w in ["desert", "sand", "dune", "dry"]):
        return "desert sand landscape"

    # 7. Mountains / Caves
    if any(w in text for w in ["mountain", "cave", "hills", "cliff", "valley"]):
        return "mountains cave path"
        
    # 8. Check for countries/cities to get real footage
    countries = ["afghanistan", "pakistan", "yemen", "saudi arabia", "iraq", "syria", "egypt", "rome", "italy", "china", "russia", "vietnam", "iran", "india", "japan"]
    for c in countries:
        if c in text:
            return f"{c.title()} city street"
            
    # 9. Fallback: Parse descriptive words from narration
    stop_words = {
        "the", "a", "an", "and", "or", "but", "if", "then", "else", "when", "where", "why", "how",
        "is", "was", "were", "are", "been", "being", "have", "has", "had", "do", "does", "did",
        "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them", "my",
        "your", "his", "its", "our", "their", "this", "that", "these", "those", "in", "on",
        "at", "by", "for", "with", "about", "against", "between", "into", "through", "during",
        "before", "after", "above", "below", "to", "from", "up", "down", "in", "out", "over",
        "under", "again", "further", "then", "once", "here", "there", "all", "any", "both",
        "each", "few", "more", "most", "other", "some", "such", "no", "nor", "not", "only",
        "own", "same", "so", "than", "too", "very", "can", "will", "just", "should", "now",
        "became", "become", "story", "shocking", "life", "first", "second", "third", "who",
        "whose", "whom", "which", "what", "that", "also", "would", "could", "should", "one", "two"
    }
    
    words = re.findall(r'[a-z]+', text)
    filtered = [w for w in words if w not in stop_words and len(w) > 3]
    if len(filtered) >= 2:
        return f"{filtered[0]} {filtered[1]}"
    elif filtered:
        return filtered[0]
        
    return topic

def segment_custom_script_local(topic, custom_script, scene_count):
    text = custom_script.strip()
    
    # Try splitting by double-newline, then single-newline, then sentences
    segments = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(segments) <= 1:
        segments = [p.strip() for p in text.split("\n") if p.strip()]
    if len(segments) <= 1:
        import re
        segments = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
        
    if not segments:
        segments = [text]
        
    scenes = []
    for idx, seg in enumerate(segments):
        words = seg.split()
        caption = " ".join(words[:3]).upper() if words else "SCENE"
        visual_query = get_visual_query(seg, topic)
        
        scenes.append({
            "title": f"Scene {idx+1}",
            "narration": seg,
            "caption": caption,
            "visual": visual_query,
            "duration": 4
        })
        
    return {
        "title": f"Custom Script: {topic}",
        "description": f"Custom script narration about {topic}.",
        "keywords": [topic],
        "scenes": scenes
    }

def write_silent_wav(duration, path):
    import wave
    sample_rate = 44100
    num_frames = int(duration * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2) # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(b'\x00' * (num_frames * 4))

@app.post("/api/upload-custom-voiceover")
def upload_custom_voiceover():
    if not is_license_activated():
        return jsonify({"error": "Key is not valid"}), 403
    draft_id = request.form.get("draftId")
    if not draft_id:
        return jsonify({"error": "Missing draftId"}), 400
        
    file = request.files.get("audio")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
        
    draft_dir = RUNS / f"draft_{draft_id}"
    draft_dir.mkdir(exist_ok=True, parents=True)
    
    ext = Path(file.filename).suffix.lower()
    if ext not in [".mp3", ".wav", ".m4a", ".ogg", ".aac", ".mp4"]:
        return jsonify({"error": "Invalid audio format"}), 400
        
    temp_dest = draft_dir / f"custom_voiceover_raw{ext}"
    file.save(temp_dest)
    
    wav_dest = draft_dir / "custom_voiceover.wav"
    try:
        ffmpeg = ffmpeg_bin()
        subprocess.run([
            ffmpeg, "-y", "-i", str(temp_dest), "-ar", "44100", "-ac", "2", str(wav_dest)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30, creationflags=SUBPROCESS_FLAGS)
        if temp_dest.exists():
            temp_dest.unlink()
    except Exception as e:
        return jsonify({"error": f"Failed to process voiceover file: {str(e)}"}), 500
        
    duration = wav_duration(wav_dest)
    return jsonify({
        "success": True,
        "duration": duration,
        "path": f"draft_{draft_id}/custom_voiceover.wav"
    })

@app.post("/api/generate-script")
def api_generate_script():
    if not is_license_activated():
        return jsonify({"error": "Key is not valid"}), 403
        
    if request.is_json:
        data = request.get_json(force=True)
    else:
        data = request.form
        
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400
        
    style = data.get("style", "documentary")
    format_name = data.get("format", "youtube")
    duration_type = data.get("duration", "1m")
    
    voice = data.get("voice", "en-US-AriaNeural")
    language = "English"
    if voice.startswith("ur-"):
        language = "Urdu"
    elif voice.startswith("hi-"):
        language = "Hindi"

    gemini_key = env_or(data.get("geminiKey"), "GEMINI_API_KEY")
    draft_id = data.get("draftId") or uuid.uuid4().hex
    
    use_custom_script = data.get("useCustomScript") == "true" or data.get("useCustomScript") is True
    custom_script = (data.get("customScript") or "").strip() if use_custom_script else ""
    use_custom_vo = data.get("useCustomVoiceover") == "true" or data.get("useCustomVoiceover") is True

    try:
        if custom_script:
            scene_map = {"15s": 4, "30s": 8, "1m": 15}
            scene_count = scene_map.get(duration_type, 15)
            script = segment_custom_script_local(title, custom_script, scene_count)
            script["isLongForm"] = False
            script["duration"] = duration_type
            script["draftId"] = draft_id
            script["voice"] = voice
        else:
            is_long_form = duration_type not in ["15s", "30s", "1m"]
            if is_long_form:
                duration_map = {
                    "5m": 5.0, "10m": 10.0, "30m": 30.0, "1h": 60.0, "2h": 120.0
                }
                duration_mins = duration_map.get(duration_type, 5.0)
                script = generate_outline(title, style, duration_mins, gemini_key, language)
                script["isLongForm"] = True
                script["duration"] = duration_type
                script["draftId"] = draft_id
                script["voice"] = voice
            else:
                scene_map = {"15s": 4, "30s": 8, "1m": 15}
                scene_count = scene_map.get(duration_type, 15)
                script = generate_script(title, style, format_name, scene_count, gemini_key, language)
                script["isLongForm"] = False
                script["duration"] = duration_type
                script["draftId"] = draft_id
                script["voice"] = voice

        # Adjust scene durations proportionally if custom voiceover was uploaded
        custom_vo_path = RUNS / f"draft_{draft_id}" / "custom_voiceover.wav"
        if use_custom_vo and custom_vo_path.exists():
            total_duration = wav_duration(custom_vo_path)
            total_chars = sum(len(str(s.get("narration", ""))) for s in script.get("scenes", []))
            if total_chars > 0:
                for scene in script["scenes"]:
                    scene_chars = len(str(scene.get("narration", "")))
                    scene["duration"] = max(round((scene_chars / total_chars) * total_duration, 2), 2.0)
            else:
                scene_dur = round(total_duration / len(script["scenes"]), 2)
                for scene in script["scenes"]:
                    scene["duration"] = scene_dur
                    
        return jsonify(script)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/upload-audio")
def upload_audio():
    if not is_license_activated():
        return jsonify({"error": "Key is not valid"}), 403
    draft_id = request.form.get("draftId")
    scene_index = request.form.get("sceneIndex")
    if not draft_id or not scene_index:
        return jsonify({"error": "Missing draftId or sceneIndex"}), 400
        
    file = request.files.get("audio")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
        
    draft_dir = RUNS / f"draft_{draft_id}"
    draft_dir.mkdir(exist_ok=True)
    
    ext = Path(file.filename).suffix.lower()
    if ext not in [".mp3", ".wav", ".m4a", ".ogg", ".aac"]:
        return jsonify({"error": "Invalid audio format"}), 400
        
    filename = f"custom_audio_{scene_index}{ext}"
    dest = draft_dir / filename
    file.save(dest)
    
    wav_filename = f"custom_audio_{scene_index}.wav"
    wav_dest = draft_dir / wav_filename
    
    try:
        ffmpeg = ffmpeg_bin()
        subprocess.run([
            ffmpeg, "-y", "-i", str(dest), "-ar", "44100", "-ac", "2", str(wav_dest)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15, creationflags=SUBPROCESS_FLAGS)
        if dest != wav_dest and dest.exists():
            dest.unlink()
    except Exception as e:
        return jsonify({"error": f"Failed to process audio: {str(e)}"}), 500
        
    return jsonify({
        "success": True,
        "audioPath": f"draft_{draft_id}/{wav_filename}",
        "duration": wav_duration(wav_dest)
    })


@app.post("/api/start")
def start():
    if not is_license_activated():
        return jsonify({"error": "Key is not valid"}), 403
    data = request.get_json(force=True)
    
    prompt = data.get("prompt", "").strip()
    title = data.get("title", "").strip()
    key_title = prompt if prompt else title
    draft_id = data.get("script", {}).get("draftId")
    
    if key_title:
        h = draft_id[:12] if draft_id else hashlib.md5(key_title.encode('utf-8')).hexdigest()[:12]
        clean_title = re.sub(r'[^a-zA-Z0-9]', '_', key_title).strip('_').lower()
        clean_title = re.sub(r'_+', '_', clean_title)[:35].strip('_')
        if clean_title:
            job_id = f"job_{clean_title}_{h}"
        else:
            job_id = f"job_{h}"
    else:
        job_id = uuid.uuid4().hex

    # Check if this job is already running
    if job_id in jobs and jobs[job_id].get("status") == "running":
        return jsonify({"jobId": job_id})

    jobs[job_id] = {
        "id": job_id, 
        "status": "running", 
        "progress": 0, 
        "message": "Queued",
        "voice": data.get("voice", "en-US-AriaNeural")
    }
    threading.Thread(target=run_job, args=(job_id, data), daemon=True).start()
    return jsonify({"jobId": job_id})


@app.post("/api/jobs/<job_id>/pause")
def pause_job(job_id):
    if not is_license_activated():
        return jsonify({"error": "Key is not valid"}), 403
    if job_id in jobs:
        jobs[job_id]["status"] = "paused"
        jobs[job_id]["message"] = "Paused by user"
        return jsonify({"ok": True})
    # If the server was restarted, the jobs dict is empty, but we can still initialize the pause state
    jobs[job_id] = {"id": job_id, "status": "paused", "message": "Paused by user"}
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/resume")
def resume_job(job_id):
    if not is_license_activated():
        return jsonify({"error": "Key is not valid"}), 403
        
    job_dir = RUNS / job_id
    payload_path = job_dir / "payload.json"
    if not payload_path.exists():
        return jsonify({"error": "Job state not found, cannot resume."}), 404
        
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"error": f"Failed to load job payload: {str(e)}"}), 500
        
    # Reset status in jobs dict
    jobs[job_id] = {
        "id": job_id,
        "status": "running",
        "progress": jobs.get(job_id, {}).get("progress", 0),
        "message": "Resuming rendering...",
        "voice": payload.get("voice", "en-US-AriaNeural")
    }
    
    # Spawn background thread to resume execution
    threading.Thread(target=run_job, args=(job_id, payload), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.get("/download/<name>")
def download(name):
    return send_file(OUTPUT / name, as_attachment=True)


import sys
import hashlib

LICENSE_FILE = RUNS / "license.json"
VALID_KEYS_FILE = RUNS / "valid_licenses.json"
SECRET_SALT = "BasixaVidToolSuperSecureLockSystem2026_Upgrade!"

def get_current_machine_id():
    import uuid
    # Mac address-based stable hardware fingerprint
    return str(uuid.getnode())

def verify_key_hash_online(key_hash):
    try:
        url = f"https://keyvalue.immanuel.co/api/KeyVal/GetValue/basixavidtoolappkey2026/basixavalid_{key_hash}"
        res = requests.get(url, timeout=7)
        if res.status_code == 200:
            val = res.text.strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            if val:
                # The online value is the signature! Verify it locally
                expected_sig = hashlib.sha256((key_hash + SECRET_SALT).encode("utf-8")).hexdigest()
                if val == expected_sig:
                    return True
    except Exception as e:
        print(f"Error validating key hash online: {e}")
    return False

def get_online_machine_id(key_hash):
    try:
        url = f"https://keyvalue.immanuel.co/api/KeyVal/GetValue/basixavidtoolappkey2026/{key_hash}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            val = res.text.strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            return val
    except Exception:
        pass
    return None

def set_online_machine_id(key_hash, machine_id):
    try:
        val = machine_id if machine_id else "none"
        url = f"https://keyvalue.immanuel.co/api/KeyVal/UpdateValue/basixavidtoolappkey2026/{key_hash}/{val}"
        requests.post(url, timeout=5)
    except Exception:
        pass

def is_license_activated():
    # Bypass license validation in cloud deployment environments
    if os.environ.get("RENDER") == "true" or os.environ.get("BYPASS_LICENSE") == "true":
        return True
    # Packaged EXE mode activation check with global online hardware ID enforcement
    if LICENSE_FILE.exists():
        try:
            data = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
            local_key = data.get("key", "").strip().upper()
            if not local_key:
                return False
                
            local_hash = hashlib.sha256(local_key.encode("utf-8")).hexdigest()
            
            # Verify online signature
            if not verify_key_hash_online(local_hash):
                return False
                
            machine_id = get_current_machine_id()
            
            # Query online database for hardware lock
            online_machine = get_online_machine_id(local_hash)
            if online_machine is not None and online_machine != "null" and online_machine != "none" and online_machine != "":
                if online_machine != machine_id:
                    # Activated on another device!
                    return False
                else:
                    return True
            else:
                # If missing online, sync it online
                set_online_machine_id(local_hash, machine_id)
                return True
        except Exception:
            pass
    return False


@app.get("/api/license-status")
def license_status():
    if os.environ.get("RENDER") == "true" or os.environ.get("BYPASS_LICENSE") == "true":
        return jsonify({
            "activated": True,
            "is_packaged": False,
            "key": "cloud_bypass"
        })
    activated = False
    local_key = ""
    
    if LICENSE_FILE.exists():
        try:
            local_key = json.loads(LICENSE_FILE.read_text(encoding="utf-8")).get("key", "")
        except Exception:
            pass
            
    if local_key:
        try:
            local_hash = hashlib.sha256(local_key.strip().upper().encode("utf-8")).hexdigest()
            if verify_key_hash_online(local_hash):
                machine_id = get_current_machine_id()
                # Check online status as well
                online_machine = get_online_machine_id(local_hash)
                if online_machine is not None and online_machine != "null" and online_machine != "none" and online_machine != "":
                    if online_machine == machine_id:
                        activated = True
                else:
                    # If missing online, sync it online
                    set_online_machine_id(local_hash, machine_id)
                    activated = True
        except Exception:
            pass
                
    return jsonify({
        "activated": activated,
        "is_packaged": True,
        "key": local_key
    })

@app.post("/api/activate")
def activate_license():
    data = request.get_json(force=True)
    key = data.get("licenseKey", "").strip().upper()
    
    if not key:
        return jsonify({"error": "Key is not valid"}), 400
        
    try:
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        
        # Verify online signature
        if not verify_key_hash_online(key_hash):
            return jsonify({"error": "Key is not valid"}), 400
            
        machine_id = get_current_machine_id()
        
        # Verify online hardware binding lock
        online_machine = get_online_machine_id(key_hash)
        if online_machine is not None and online_machine != "null" and online_machine != "none" and online_machine != "":
            if online_machine != machine_id:
                return jsonify({"error": "This License Key has already been activated on another computer!"}), 400
        
        # Save online and locally
        set_online_machine_id(key_hash, machine_id)
        
        LICENSE_FILE.write_text(json.dumps({
            "activated": True,
            "key": key
        }, indent=2), encoding="utf-8")
        return jsonify({"success": True})
        
    except Exception as e:
        return jsonify({"error": f"Failed to activate: {str(e)}"}), 500

@app.post("/api/deactivate")
def deactivate_license():
    try:
        if LICENSE_FILE.exists():
            try:
                local_data = json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
                key = local_data.get("key", "").strip().upper()
                if key:
                    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
                    # Deactivate online binding lock
                    set_online_machine_id(key_hash, "none")
            except Exception:
                pass
            try:
                LICENSE_FILE.unlink()
            except Exception:
                pass
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": f"Failed to deactivate: {str(e)}"}), 500


@app.post("/api/delete")
def delete_video():
    try:
        data = request.json or {}
        filename = data.get("fileName")
        if not filename:
            return jsonify({"error": "Filename is required"}), 400
        file_path = OUTPUT / filename
        if file_path.exists() and file_path.parent == OUTPUT:
            file_path.unlink()
            raw_path = OUTPUT / filename.replace(".mp4", "_raw.mp4")
            if raw_path.exists():
                raw_path.unlink()
            return jsonify({"success": True})
        return jsonify({"error": "File not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/shutdown")
def shutdown():
    import os
    import signal
    os.kill(os.getpid(), signal.SIGTERM)
    return jsonify({"success": True})


def open_browser():
    import webbrowser
    import time
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:7860/")


if __name__ == "__main__":
    import threading
    threading.Thread(target=open_browser, daemon=True).start()
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
