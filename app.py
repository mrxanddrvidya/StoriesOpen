import streamlit as st
import time
import queue
import re
import json
import os
import random
import string
import base64
import requests
import asyncio
import tempfile
import zipfile
import platform
import subprocess
import atexit
import io
import threading
from pathlib import Path
from openai import OpenAI
from datetime import datetime
import edge_tts
import backoff

# ------------------- Page config -------------------
st.set_page_config(page_title="SG Story Generator", page_icon="📖", layout="wide")

# ------------------- Mac sleep prevention (always on) -------------------
_caffeinate_proc = None

def start_caffeinate():
    global _caffeinate_proc
    if platform.system() == "Darwin" and _caffeinate_proc is None:
        try:
            _caffeinate_proc = subprocess.Popen(["caffeinate", "-i", "-d"],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass

def stop_caffeinate():
    global _caffeinate_proc
    if _caffeinate_proc:
        _caffeinate_proc.terminate()
        _caffeinate_proc = None

atexit.register(stop_caffeinate)

# Start caffeinate immediately (always on)
if platform.system() == "Darwin":
    start_caffeinate()

# ------------------- Text Cleaning Function (Removes asterisks and markdown) -------------------
def clean_text_for_tts(text):
    """Remove markdown formatting and special characters that TTS might read aloud."""
    # Remove markdown bold and italic markers
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # **bold** -> bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)      # *italic* -> italic
    text = re.sub(r'__([^_]+)__', r'\1', text)      # __bold__ -> bold
    text = re.sub(r'_([^_]+)_', r'\1', text)        # _italic_ -> italic
    
    # Remove markdown headings
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    
    # Remove markdown links [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    
    # Remove horizontal rules
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\*{3,}$', '', text, flags=re.MULTILINE)
    
    # Remove excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' +', ' ', text)
    
    # Remove asterisks used as list markers
    text = re.sub(r'^\*\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    
    # Remove standalone asterisks
    text = re.sub(r'\*', '', text)
    
    # Remove other special characters that TTS might read
    text = re.sub(r'[#~`>]', '', text)
    
    # Trim whitespace
    text = text.strip()
    
    return text

def clean_text_for_display(text):
    """Clean text for display on screen (keep some formatting, remove asterisks)."""
    # Remove markdown bold/italic markers but keep the text
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    
    # Remove markdown headings (keep the text)
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    
    # Remove standalone asterisks
    text = re.sub(r'\*', '', text)
    
    return text

def clean_garbage_output(text):
    """Remove poetic garbage and fix common issues in generated text."""
    lines = text.split('\n')
    cleaned_lines = []
    
    garbage_indicators = [
        'crimson', 'tendrils', 'cascading', 'vertebrae', 'spectral',
        'metamorphosis', 'cacophony', 'symbiotic', 'infinitum',
        'visceral', 'ethereal', 'labyrinthine', 'phantasm',
        'threshold', 'fracturing', 'effervescent', 'precipice',
        'dissonance', 'juxtaposition', 'quintessential', 'fragmented',
        'silver-coated', 'skeletal', 'boundless', 'unforgiving'
    ]
    
    for line in lines:
        # Skip lines that are too long (>200 chars) and contain garbage
        if len(line) > 200 and any(word in line.lower() for word in garbage_indicators):
            continue
        # Skip lines with multiple garbage words
        garbage_count = sum(1 for word in garbage_indicators if word in line.lower())
        if garbage_count > 2:
            continue
        cleaned_lines.append(line)
    
    result = '\n'.join(cleaned_lines)
    
    # If result is too short, return original
    if len(result.split()) < len(text.split()) * 0.5:
        return text
    return result

# ------------------- Session State -------------------
if "story_content" not in st.session_state:
    st.session_state.story_content = ""
if "original_story" not in st.session_state:
    st.session_state.original_story = ""
if "timestamp" not in st.session_state:
    st.session_state.timestamp = int(time.time())
if "generation_error" not in st.session_state:
    st.session_state.generation_error = None
if "batch_generating" not in st.session_state:
    st.session_state.batch_generating = False
if "last_gen_stats" not in st.session_state:
    st.session_state.last_gen_stats = None
if "story_id" not in st.session_state:
    st.session_state.story_id = f"{int(time.time())}_{''.join(random.choices(string.digits, k=4))}"
if "extracted_premise" not in st.session_state:
    st.session_state.extracted_premise = ""
if "batch_stories" not in st.session_state:
    st.session_state.batch_stories = []
if "batch_outputs" not in st.session_state:
    st.session_state.batch_outputs = []
if "generated_mp3_path" not in st.session_state:
    st.session_state.generated_mp3_path = None
if "generated_mp3_title" not in st.session_state:
    st.session_state.generated_mp3_title = ""

def get_checkpoint_file():
    return f"story_checkpoint_{st.session_state.story_id}.json"

st.title("📖 SG Story Generator")
st.markdown("*Batch story generation with automatic email delivery and MP3 audiobook*")

# ------------------- Fixed Settings (Hardcoded - No Sidebar) -------------------
SLOW_BURN_MODE = True
USE_CAFFEINATE = True
TONE = "Brutal"
ADULT_LEVEL = 10
NUM_CHAPTERS = 6
EDGE_VOICE = "en-IN-NeerjaNeural"

# ------------------- Default Feminine Story Elements -------------------
DEFAULT_ELEMENTS = [
    "Lace panties and bras", "Feeling of lace against skin", "HRT - estrogen pills",
    "Breast development", "Waist training corset", "High heels training",
    "Saree draping", "Salwar kameez", "Lehenga", "Indian jewelry",
    "Breast play and nipple sucking", "Blow jobs while kneeling",
    "Public outings as a woman", "Ear piercing", "Nose piercing",
    "Lipstick - trying different shades", "Eye makeup", "Nail polish", "Bangles", "Anklets"
]

# ------------------- OpenRouter API Configuration -------------------
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Updated model list - try these in order
OPENROUTER_MODELS = [
    "openrouter/auto",  # auto-selects best available (recommended)
    "anthropic/claude-3-haiku-20240307",  # paid but fast
    "mistralai/mixtral-8x7b-instruct",  # good quality
    "google/gemini-pro",  # good quality
    "meta-llama/llama-3-8b-instruct",  # free tier
    "cognitivecomputations/dolphin-mixtral-8x7b:free"  # older free model
]

def call_openrouter(prompt, max_tokens=3500, temperature=0.7, retries=3):
    """Call OpenRouter with multiple model fallbacks."""
    
    # Debug: Check if API key exists
    api_key = os.getenv("OPENROUTER_API_KEY")
    
    if not api_key:
        # Try alternative environment variable names
        api_key = os.getenv("OPENROUTER_KEY")
    if not api_key:
        api_key = os.getenv("OR_API_KEY")
    
    if not api_key:
        return None, "❌ OPENROUTER_API_KEY not found in secrets. Make sure it's added exactly as 'OPENROUTER_API_KEY'"
    
    # Debug: Show key format (first few chars)
    print(f"API Key found: {api_key[:15]}... (length: {len(api_key)})")
    
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key, timeout=120)
    
    system_prompt = """You are a direct, no-nonsense creative writer. You write adult fiction in clear, simple English. You NEVER use metaphors, similes, or poetic language. You write short sentences. You describe actions and dialogue directly. You NEVER write abstract nonsense. Write simply and directly. No poetry. No metaphors."""
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt}
    ]
    
    last_error = None
    
    for model in OPENROUTER_MODELS:
        for attempt in range(retries):
            try:
                print(f"Trying model: {model} (attempt {attempt + 1})")
                
                completion = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    frequency_penalty=0.5,
                    presence_penalty=0.5,
                    stream=False,
                    extra_headers={
                        "HTTP-Referer": "https://story-generator.app",
                        "X-Title": "Story Generator"
                    }
                )
                
                text = completion.choices[0].message.content
                if text and len(text.strip()) > 200:
                    print(f"Success with model: {model}")
                    text = clean_garbage_output(text)
                    return text, None
                    
            except Exception as e:
                error_msg = str(e)
                print(f"Model {model} failed: {error_msg[:100]}")
                last_error = error_msg
                
                # Check for specific errors
                if "authentication" in error_msg.lower() or "api key" in error_msg.lower():
                    return None, f"Authentication error: {error_msg[:100]}"
                if "insufficient_quota" in error_msg.lower() or "credits" in error_msg.lower():
                    return None, f"Insufficient credits: {error_msg[:100]}"
                    
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                continue
    
    return None, f"All models failed. Last error: {last_error[:200]}"


def generate_with_progress(prompt, max_tokens, step_description):
    with st.spinner(f"📝 {step_description}..."):
        result, err = call_openrouter(prompt, max_tokens)
    return result, err

# ------------------- Test API -------------------
def test_api():
    """Test API with detailed error reporting."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    
    if not api_key:
        api_key = os.getenv("OPENROUTER_KEY")
    if not api_key:
        api_key = os.getenv("OR_API_KEY")
    
    if not api_key:
        return False, "OPENROUTER_API_KEY not found in secrets. Add it as 'OPENROUTER_API_KEY' in Space Settings → Secrets"
    
    # Show key format for debugging
    st.code(f"Key format: {api_key[:15]}... (length: {len(api_key)})\nExpected format: sk-or-v1-...")
    
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key, timeout=30)
    
    # Try multiple models for testing
    test_models = ["openrouter/auto", "openrouter/free", "mistralai/mixtral-8x7b-instruct"]
    
    for model in test_models:
        try:
            st.info(f"Testing model: {model}...")
            completion = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
                max_tokens=5,
                temperature=0.0
            )
            reply = completion.choices[0].message.content.strip()
            if reply == "OK" or "OK" in reply:
                return True, f"API works with {model}!"
        except Exception as e:
            st.warning(f"Model {model} failed: {str(e)[:100]}")
            continue
    
    return False, "All models failed. Check your API key and credits at https://openrouter.ai/keys"

# ------------------- Story Generation Functions -------------------
def generate_global_outline(num_chapters, topic):
    outline_prompt = f"""
Create a detailed outline for a {num_chapters}-chapter story based on this premise:

Premise: {topic}

Tone: Brutal, explicitness level: 10/10.

CRITICAL: Chapter 1 is pure tension, NO physical feminisation yet.

Now produce:
1. A compelling TITLE
2. A SUMMARY (300 words)
3. For each chapter (1 to {num_chapters}), a paragraph describing key events

Return ONLY these sections, no extra commentary.

WRITE SIMPLY AND DIRECTLY. NO POETRY. NO METAPHORS.
"""
    return generate_with_progress(outline_prompt, max_tokens=2500, step_description="Generating story outline")

def generate_single_chapter(chapter_num, outline, previous_chapter_text):
    context = ""
    if previous_chapter_text:
        words = previous_chapter_text.split()
        context = ' '.join(words[-300:]) if len(words) > 300 else previous_chapter_text

    # STRONG STYLE ENFORCEMENT - prevents garbage/poetic output
    style_instructions = """
CRITICAL STYLE RULES - MUST FOLLOW:
- Write in SHORT, SIMPLE sentences (10-20 words max)
- NO metaphors, NO similes, NO poetic descriptions
- NO phrases like "crimson blade thrust" or "harrowing threshold"
- Write DIRECT action: "He did X. She said Y."
- Use plain English: "He was scared." NOT "Fear cascaded through his trembling vertebrae"
- Dialogue should be realistic, not dramatic
- Describe sex directly: "He entered her from behind." NOT "their bodies merged in ecstatic synergy"
- Be CONCRETE, not abstract
- Imagine you are writing for a newspaper, not a literary magazine
- If a sentence sounds like poetry, DELETE IT and rewrite simply

FORBIDDEN words/phrases (do not use these):
- "threshold", "crimson", "tendrils", "cascading", "fracturing", "vertebrae"
- "spectral", "metamorphosis", "cacophony", "symbiotic", "infinitum"
- "visceral", "ethereal", "labyrinthine", "effervescent", "phantasm"
- "fragmented", "silver-coated", "skeletal", "boundless", "unforgiving"

REQUIRED writing style:
- Subject-Verb-Object structure
- Character does action: "Rahul walked to the door."
- Character feels emotion simply: "He was nervous."
- Character speaks plainly: "I'm scared," he said.
"""

    chapter_prompt = f"""
Write an intense, explicit feminisation novel. Follow the outline.

OUTLINE:
{outline}

Now write CHAPTER {chapter_num}. Target length: approximately 800-1000 words.

Previous chapter's ending:
{context}

**STYLE RULES - READ CAREFULLY:**
{style_instructions}

**MANDATORY ELEMENTS:**
- Bra & nipple scene – unhooking bra, fondling, sucking, breast pump.
- Blow job – kneeling, deepthroat.
- Anal sex from behind.

**Naming rule:** Before name change: use male name and he/him. After: use feminine name and she/her.

**Tone & explicitness:** Brutal (level 10/10) – raw, brutal, sexually explicit.

**CRITICAL: NO POETRY. NO ABSTRACT METAPHORS. JUST SAY WHAT HAPPENS.**

Write the chapter now. Be DIRECT and SIMPLE:
"""
    return generate_with_progress(chapter_prompt, max_tokens=2500, step_description=f"Writing Chapter {chapter_num}")

def combine_chapters(chapters, outline):
    title_match = re.search(r"TITLE:\s*(.+?)(?:\n|$)", outline, re.IGNORECASE)
    story_title = title_match.group(1).strip() if title_match else "Untitled Story"
    full_story = f"TITLE: {story_title}\n\n"
    for i, chapter in enumerate(chapters, 1):
        chapter_text = re.sub(r"^\s*Chapter\s+\d+.*?\n", "", chapter, flags=re.IGNORECASE) if chapter else "[Failed]"
        # Clean story content for display
        chapter_text = clean_text_for_display(chapter_text)
        full_story += f"## Chapter {i}\n\n{chapter_text.strip()}\n\n"
    return full_story

def generate_complete_story(topic):
    """Generate a complete story from a premise."""
    st.info(f"📖 Generating outline for: {topic[:80]}...")
    outline, err = generate_global_outline(NUM_CHAPTERS, topic)
    if err:
        return None, f"Outline failed: {err}"
    
    chapters_done = []
    prev_text = ""
    total_start = time.time()
    
    for ch in range(1, NUM_CHAPTERS + 1):
        st.info(f"✍️ Writing Chapter {ch} of {NUM_CHAPTERS}...")
        start_ch = time.time()
        chapter_text, err = generate_single_chapter(ch, outline, prev_text)
        if err or not chapter_text:
            return None, f"Chapter {ch} failed: {err or 'empty response'}"
        
        # CLEAN GARBAGE OUTPUT from chapter
        chapter_text = clean_garbage_output(chapter_text)
        
        chapters_done.append(chapter_text)
        prev_text = chapter_text
        elapsed = time.time() - start_ch
        st.success(f"Chapter {ch} done in {elapsed:.1f}s")
    
    total_time = time.time() - total_start
    full_story = combine_chapters(chapters_done, outline)
    full_story = f"**Premise:** {topic}\n\n---\n\n{full_story}"
    word_count = len(full_story.split())
    stats = {"total_time": total_time, "word_count": word_count, "chapters": len(chapters_done)}
    
    return full_story, stats

# ------------------- MP3 Generation (with text cleaning) -------------------
def generate_mp3_sync(text, story_title, timestamp, voice="en-IN-NeerjaNeural"):
    """Generate MP3 synchronously and return the file path. Text is cleaned before TTS."""
    # Clean text before sending to TTS
    clean_text = clean_text_for_tts(text)
    
    temp_dir = tempfile.gettempdir()
    safe_title = re.sub(r'[<>:"/\\|?*]', '', story_title.replace(' ', '_'))
    mp3_path = os.path.join(temp_dir, f"{safe_title}_{timestamp}.mp3")
    
    async def generate_async():
        communicate = edge_tts.Communicate(clean_text, voice)
        await communicate.save(mp3_path)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(generate_async())
    loop.close()
    
    return mp3_path

def send_mp3_email_background(story_content, story_title, index, timestamp, voice):
    """Background thread function to generate MP3 and send email."""
    try:
        # Clean story content before MP3 generation
        clean_story = clean_text_for_tts(story_content)
        
        # Generate MP3
        mp3_path = generate_mp3_sync(clean_story, story_title, timestamp, voice)
        
        # Send email with MP3 attachment
        send_story_email(story_content, story_title, index, mp3_path)
        
        # Clean up
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
            
        st.success(f"🎵 MP3 audiobook for story {index} has been emailed!")
    except Exception as e:
        st.warning(f"MP3 generation failed for story {index}: {e}")

# ------------------- Email Function -------------------
def send_story_email(story_content, story_title, index, mp3_path=None):
    """Send story with TXT and optionally MP3 attachments."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        return False, "No API key"
    
    # Clean story content for email display
    story_clean = clean_text_for_display(story_content)
    story_clean = story_clean.encode('utf-8', 'ignore').decode('utf-8')
    story_title_clean = story_title.encode('utf-8', 'ignore').decode('utf-8')[:100]
    
    attachments = [
        {"filename": f"story_{index}.txt", "content": base64.b64encode(story_clean.encode("utf-8")).decode("utf-8"), "encoding": "base64"}
    ]
    
    # Add MP3 if available
    has_mp3 = False
    if mp3_path and os.path.exists(mp3_path):
        with open(mp3_path, "rb") as f:
            mp3_content = base64.b64encode(f.read()).decode("utf-8")
        attachments.append({"filename": f"story_{index}.mp3", "content": mp3_content, "encoding": "base64"})
        has_mp3 = True
    
    subject_suffix = " + MP3" if has_mp3 else ""
    
    payload = {
        "from": "onboarding@resend.dev",
        "to": "bhuyan.pradip@gmail.com",
        "subject": f"Story {index}: {story_title_clean}{subject_suffix}",
        "text": f"Your story #{index} ({story_title_clean}) is attached.{' MP3 audiobook included.' if has_mp3 else ''}",
        "attachments": attachments
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        r = requests.post("https://api.resend.com/emails", json=payload, headers=headers)
        return (r.status_code == 200), r.text if r.status_code != 200 else None
    except Exception as e:
        return False, str(e)

# ------------------- Batch Processing -------------------
def parse_story_file(uploaded_file):
    """Parse uploaded text file into individual story snippets."""
    content = uploaded_file.getvalue().decode("utf-8")
    if '---' in content:
        snippets = [s.strip() for s in content.split('---') if s.strip()]
    else:
        snippets = [s.strip() for s in content.split('\n\n') if s.strip()]
    return snippets

def process_batch_stories(snippets):
    """Process multiple stories in batch - TXT emailed immediately, MP3 in background."""
    results = []
    total = len(snippets)
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    try:
        for i, snippet in enumerate(snippets):
            # Clean premise
            clean_snippet = re.sub(r'^Snippet\s+[\d\.]+\s*[–\-]\s*', '', snippet)
            clean_snippet = clean_snippet.strip()
            
            status_text.text(f"Processing story {i+1} of {total}: {clean_snippet[:80]}...")
            
            # Generate story
            story, stats = generate_complete_story(clean_snippet)
            
            if story:
                # Extract title
                title_match = re.search(r"TITLE:\s*(.+?)(?:\n|$)", story, re.IGNORECASE)
                story_title = title_match.group(1).strip() if title_match else f"Story {i+1}"
                timestamp = st.session_state.timestamp
                
                # Send TXT email immediately (NO MP3 yet)
                email_sent, msg = send_story_email(story, story_title, i+1, mp3_path=None)
                
                # Start background thread for MP3 generation (non-blocking)
                thread = threading.Thread(
                    target=send_mp3_email_background,
                    args=(story, story_title, i+1, timestamp, EDGE_VOICE),
                    daemon=True
                )
                thread.start()
                
                results.append({
                    "index": i+1,
                    "premise": clean_snippet,
                    "title": story_title,
                    "word_count": stats["word_count"],
                    "email_sent": email_sent,
                    "mp3_started": True
                })
                
                if email_sent:
                    st.success(f"✅ Story {i+1} completed and emailed! (MP3 generating in background)")
                else:
                    st.warning(f"⚠️ Story {i+1} completed but email failed: {msg}")
            else:
                results.append({
                    "index": i+1,
                    "premise": clean_snippet,
                    "error": stats
                })
                st.error(f"❌ Story {i+1} failed: {stats}")
            
            progress_bar.progress((i+1)/total)
    finally:
        pass  # caffeinate stays on
    
    status_text.text("Batch processing complete!")
    return results

# ------------------- UI -------------------
st.subheader("📁 Batch Story Input")

uploaded_file = st.file_uploader(
    "Upload a text file with story premises (separate each story with '---' on a new line)",
    type=["txt"],
    help="Example:\nStory premise one...\n---\nStory premise two...\n---\nStory premise three..."
)

if uploaded_file:
    snippets = parse_story_file(uploaded_file)
    st.success(f"✅ Found {len(snippets)} story premises in the file")
    
    with st.expander("📝 Preview Story Premises", expanded=False):
        for i, snippet in enumerate(snippets):
            clean = re.sub(r'^Snippet\s+[\d\.]+\s*[–\-]\s*', '', snippet)
            st.write(f"**Story {i+1}:** {clean[:100]}...")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🚀 Start Batch Generation", type="primary", use_container_width=True):
            if not snippets:
                st.warning("No valid story premises found in the file.")
            else:
                st.session_state.batch_stories = snippets
                st.session_state.batch_generating = True
                st.rerun()
    with col2:
        if st.button("🗑️ Clear", use_container_width=True):
            st.session_state.batch_stories = []
            st.session_state.batch_outputs = []
            st.rerun()

# ------------------- Test API Button -------------------
col1, col2, col3 = st.columns([1, 1, 2])
with col1:
    if st.button("🔑 Test API", use_container_width=True):
        with st.spinner("Testing..."):
            ok, msg = test_api()
            if ok:
                st.success(f"✅ {msg}")
            else:
                st.error(f"❌ {msg}")
                st.info("Add OPENROUTER_API_KEY in Space Settings → Repository secrets")

# ------------------- Single Story Option -------------------
st.markdown("---")
st.subheader("📝 Single Story (Optional)")

single_premise = st.text_area(
    "Or enter a single story premise here",
    height=60,
    placeholder="Story here.."
)

if st.button("✨ Generate Single Story", type="secondary", use_container_width=True):
    if not single_premise.strip():
        st.warning("Please enter a story premise.")
    else:
        try:
            story, stats = generate_complete_story(single_premise)
            if story:
                title_match = re.search(r"TITLE:\s*(.+?)(?:\n|$)", story, re.IGNORECASE)
                story_title = title_match.group(1).strip() if title_match else "Single Story"
                timestamp = st.session_state.timestamp
                
                # Send TXT email immediately
                email_sent, msg = send_story_email(story, story_title, 1, mp3_path=None)
                if email_sent:
                    st.success("📧 Story emailed (TXT)!")
                else:
                    st.warning(f"Email failed: {msg}")
                
                # Start background MP3 generation
                thread = threading.Thread(
                    target=send_mp3_email_background,
                    args=(story, story_title, 1, timestamp, EDGE_VOICE),
                    daemon=True
                )
                thread.start()
                st.info("🎵 MP3 audiobook generation started in background. You will receive it via email when ready.")
                
                # Provide download button for TXT
                st.download_button("💾 Download Story (TXT)", data=story,
                                   file_name=f"story_{timestamp}.txt", use_container_width=True)
                
                st.session_state.story_content = story
                st.session_state.last_gen_stats = stats
                st.success(f"✅ Story complete! {stats['word_count']:,} words")
                st.rerun()
            else:
                st.error(f"Story generation failed: {stats}")
        except Exception as e:
            st.error(f"Error: {e}")

# ------------------- Batch Generation Runner -------------------
if st.session_state.batch_generating and st.session_state.batch_stories:
    st.session_state.batch_generating = False
    
    st.subheader("📊 Batch Generation Progress")
    
    results = process_batch_stories(st.session_state.batch_stories)
    st.session_state.batch_outputs = results
    
    # Display summary
    st.markdown("---")
    st.subheader("📊 Batch Summary")
    
    success_count = len([r for r in results if r.get("word_count")])
    fail_count = len([r for r in results if r.get("error")])
    email_count = len([r for r in results if r.get("email_sent")])
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Stories", len(results))
    col2.metric("Successful", success_count)
    col3.metric("Emailed (TXT)", email_count)
    col4.metric("Failed", fail_count)
    
    st.info("🎵 MP3 audiobooks are being generated in the background. You will receive them via email when ready.")
    
    # Download all stories as ZIP
    if success_count > 0:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for r in results:
                if r.get("title"):
                    safe_title = re.sub(r'[<>:"/\\|?*]', '', r['title'][:50])
                    # Clean story before saving to ZIP
                    clean_story = clean_text_for_display(r.get("story", ""))
                    zip_file.writestr(f"story_{r['index']:03d}_{safe_title}.txt", f"Title: {r['title']}\n\nWord count: {r['word_count']}\n\n{clean_story}")
        zip_buffer.seek(0)
        
        st.download_button(
            label="📦 Download Summary (TXT)",
            data=zip_buffer,
            file_name=f"stories_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
            use_container_width=True
        )
    
    # Display individual results
    with st.expander("📄 Detailed Results", expanded=True):
        for r in results:
            if r.get("title"):
                st.success(f"**Story {r['index']}:** {r['title']} - {r['word_count']} words | Email (TXT): {'✅' if r.get('email_sent') else '❌'} | MP3: 🔄 Background")
            else:
                st.error(f"**Story {r['index']}:** Failed - {r.get('error', 'Unknown error')}")
    
    st.session_state.batch_stories = []

# ------------------- Display Generated Story (for single mode) -------------------
if st.session_state.story_content and not st.session_state.batch_outputs:
    st.subheader("📖 Generated Story")
    # Clean story for display
    display_story = clean_text_for_display(st.session_state.story_content)
    # Also remove any remaining garbage
    display_story = clean_garbage_output(display_story)
    st.write(display_story[:5000])
    
    if len(display_story) > 5000:
        st.info("Story truncated for display. Download the full story below.")
    
    if st.session_state.last_gen_stats:
        st.caption(f"📊 {st.session_state.last_gen_stats.get('word_count', 0):,} words | {st.session_state.last_gen_stats.get('chapters', 0)} chapters")
    
    col1, col2 = st.columns(2)
    with col1:
        st.download_button("💾 Download Story (TXT)", data=st.session_state.story_content,
                           file_name=f"story_{st.session_state.timestamp}.txt", use_container_width=True)
    with col2:
        if st.button("🆕 Clear", use_container_width=True):
            st.session_state.story_content = ""
            st.session_state.last_gen_stats = None
            st.rerun()

# Keep caffeinate running
if platform.system() == "Darwin":
    st.sidebar.caption("☕ Caffeinate active - Mac will not sleep")
