#!/usr/bin/env python3
"""台本 → Fish Audio 音声 → 動画素材 → テロップ → BGM → 縦型リール を一括生成する。

使い方:
  export FISH_AUDIO_API_KEY=...          # Fish Audio の API キー
  python3 tools/make_reel.py --model-id <Fish AudioのモデルID>

主なオプション（既定値は台本テンプレートの例どおり）:
  --script scripts/script01.txt  --audio audio/script01.mp3
  --materials materials/  --bgm bgm/  --out output/script01_final.mp4
  --font-size 60  --bgm-volume 0.15  --max-chars 13
  --preview   APIキーやモデルIDがなくても、無音・推定尺で仮の動画を作る

素材フォルダ: ファイル名先頭の番号順（01_xxx.mp4, 2-yyy.mov ...）。
  "_" で始まるファイルは無視する。動画のほか画像(jpg/png)も1カットとして使える。
  素材が1つもない場合は、仮の単色背景で書き出す。
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
COLORS = ["#FFFFFF", "#FFE600", "#00E5FF", "#FF9900"]  # 白・黄・シアン・オレンジ
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# 読み上げ用の置き換え（テロップはそのまま、音声だけ読みやすくする）
READINGS = [
    ("OBgE（オブジェ）", "オブジェ"), ("OBgE", "オブジェ"),
    ("SPF50+", "エスピーエフ50プラス"), ("PA++++", "ピーエー、プラスフォー"),
    ("18g", "18グラム"), ("／", "、"), ("・", "、"),
]
# 読み上げない行（注記はテロップのみ表示）
TELOP_ONLY = re.compile(r"^※")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return r.stdout


def duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=nw=1:nk=1", str(path)]).strip())


def clean_line(s):
    s = re.sub(r"[☀-➿\U0001F000-\U0001FAFF️]", "", s)  # 絵文字・✔
    return s.replace("【PR】", "").strip()


def load_lines(script_path):
    lines = []
    for raw in Path(script_path).read_text(encoding="utf-8").splitlines():
        s = clean_line(raw)
        if not s or s.startswith("#"):
            continue
        lines.append(s)
    return lines


def speech_of(line):
    if TELOP_ONLY.match(line):
        return ""
    for a, b in READINGS:
        line = line.replace(a, b)
    return line


BREAK_AFTER = "、。…！？!?）」・"
PARTICLES = "はがをにでのもてとへやよね"


def split_telop(text, n):
    """最大 n 文字で、句読点 → 助詞の後 → 機械的、の優先順で自然に区切る。"""
    text = text.strip()
    if len(text) <= n:
        return [text]
    best = None
    for i in range(min(n, len(text) - 1), 0, -1):  # 句読点の直後
        if text[i - 1] in BREAK_AFTER and len(text) - i > 2:
            best = i; break
    if best is None or best < n * 0.4:
        for i in range(min(n, len(text) - 1), max(1, int(n * 0.4)) - 1, -1):
            # 助詞の直後で、次が助詞でない位置
            if text[i - 1] in PARTICLES and text[i] not in PARTICLES + BREAK_AFTER \
                    and len(text) - i > 2:
                best = i; break
    if best is None or best < n * 0.4:
        # カタカナ語・英数字の途中を避ける
        best = n
        for i in range(n, max(1, int(n * 0.5)) - 1, -1):
            a, b = text[i - 1], text[i]
            if not (re.match(r"[ァ-ヶー]", a) and re.match(r"[ァ-ヶー]", b)) and \
               not (re.match(r"[A-Za-z0-9+]", a) and re.match(r"[A-Za-z0-9+]", b)):
                best = i; break
    head = text[:best].rstrip("、・")
    return [head] + split_telop(text[best:], n)


def fish_tts(text, model_id, out_path, api_key, backend):
    req = urllib.request.Request(
        "https://api.fish.audio/v1/tts",
        data=json.dumps({"text": text, "reference_id": model_id, "format": "mp3",
                         "normalize": True, "latency": "normal"}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "model": backend})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    if len(data) < 1000:
        sys.exit(f"Fish Audio の応答が音声ではありません: {data[:300]!r}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(data)


def numbered(folder, exts):
    files = []
    for p in Path(folder).glob("*") if Path(folder).exists() else []:
        if p.name.startswith(("_", ".")) or p.suffix.lower() not in exts:
            continue
        m = re.match(r"\s*(\d+)", p.name)
        files.append((int(m.group(1)) if m else 10**9, p.name, p))
    return [p for _, _, p in sorted(files)]


def telop_png(text, color, size, path):
    font = ImageFont.truetype(FONT, size)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    box = d.textbbox((0, 0), text, font=font, stroke_width=4)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((W - tw) / 2 - box[0], (H - th) / 2 - box[1]), text, font=font, fill=color,
           stroke_width=4, stroke_fill="black")
    img.save(path)


def pr_badge(path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 40)
    d.rounded_rectangle((60, 220, 160, 280), 10, fill=(0, 0, 0, 170))
    b = d.textbbox((0, 0), "PR", font=font)
    d.text((110 - (b[2] - b[0]) / 2 - b[0], 250 - (b[3] - b[1]) / 2 - b[1]), "PR",
           font=font, fill="white")
    img.save(path)


def make_cut(src, dur, out):
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"fps={FPS},setsar=1,format=yuv420p")
    if src is None:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0x2a2f33:s={W}x{H}:r={FPS}"]
    elif src.suffix.lower() in IMAGE_EXT:
        # 静止画はゆっくりズームして動きをつける
        frames = int(round(dur * FPS))
        vf = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
              f"zoompan=z='1+0.08*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={W}x{H}:fps={FPS},setsar=1,format=yuv420p")
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(src)]
    else:
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src)]  # 短い素材はループ
    run(cmd + ["-t", f"{dur:.3f}", "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "18", str(out)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default="scripts/script01.txt")
    ap.add_argument("--audio", default="audio/script01.mp3")
    ap.add_argument("--materials", default="materials")
    ap.add_argument("--bgm", default="bgm")
    ap.add_argument("--out", default="output/script01_final.mp4")
    ap.add_argument("--model-id", default=os.environ.get("FISH_AUDIO_MODEL_ID"))
    ap.add_argument("--tts-backend", default="s1", help="Fish Audio の model ヘッダー")
    ap.add_argument("--font-size", type=int, default=60)
    ap.add_argument("--max-chars", type=int, default=13)
    ap.add_argument("--bgm-volume", type=float, default=0.15)
    ap.add_argument("--chars-per-sec", type=float, default=7.0, help="--preview 時の推定話速")
    ap.add_argument("--reuse-audio", action="store_true", help="既存の音声ファイルを使う")
    ap.add_argument("--preview", action="store_true")
    a = ap.parse_args()

    lines = load_lines(a.script)
    speech = "\n".join(s for s in (speech_of(l) for l in lines) if s)
    Path(a.script).with_suffix(".speech.txt").write_text(speech + "\n", encoding="utf-8")

    # 1) 音声
    audio = Path(a.audio)
    if a.reuse_audio and audio.exists():
        total = duration(audio)
    elif not a.preview:
        key = os.environ.get("FISH_AUDIO_API_KEY")
        if not key or not a.model_id:
            sys.exit("FISH_AUDIO_API_KEY と --model-id が必要です（仮動画なら --preview）")
        fish_tts(speech, a.model_id, audio, key, a.tts_backend)
        total = duration(audio)
    else:
        audio = None
        total = sum(len(speech_of(l)) for l in lines) / a.chars_per_sec

    # 2) テロップの時間割り当て（読み上げ文字数に比例。注記行は短く固定）
    units = []  # (telop_text, weight)
    for l in lines:
        sp = speech_of(l)
        parts = split_telop(l, a.max_chars)
        if sp:
            w_line = len(sp)
            sizes = [len(p) for p in parts]
            units += [(p, w_line * s / sum(sizes)) for p, s in zip(parts, sizes)]
        else:
            units += [(p, 0.0) for p in parts]
    note_time = 1.2  # 読み上げない注記の表示時間
    n_notes = sum(1 for _, w in units if w == 0)
    voiced = sum(w for _, w in units)
    total_video = total + note_time * n_notes + 0.5  # 最後に少し余韻
    timeline, t = [], 0.0
    for i, (text, w) in enumerate(units):
        d = note_time if w == 0 else total * w / voiced
        timeline.append({"text": text, "start": round(t, 3), "end": round(t + d, 3),
                         "color": COLORS[i % len(COLORS)]})
        t += d
    timeline[-1]["end"] = round(total_video, 3)

    # 注記の時間は音声に無音を挟んで合わせる
    work = Path(tempfile.mkdtemp(prefix="reel_"))
    voice_track = None
    if audio:
        # 音声を「読み上げ行」ごとに比例配分したまま、注記の位置に無音を差し込む
        pieces, a_t = [], 0.0
        for (text, w), u in zip(units, timeline):
            if w == 0:
                pieces.append(("sil", note_time))
            else:
                d = total * w / voiced
                if pieces and pieces[-1][0] == "voice":
                    pieces[-1] = ("voice", pieces[-1][1], pieces[-1][2] + d)
                else:
                    pieces.append(("voice", a_t, d))
                a_t += d
        chains, labels = [], []
        for k, p in enumerate(pieces):
            if p[0] == "sil":
                chains.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{p[1]:.3f}[p{k}]")
            else:
                chains.append(f"[0:a]atrim={p[1]:.3f}:{p[1]+p[2]:.3f},asetpts=PTS-STARTPTS,"
                              f"aformat=sample_rates=44100:channel_layouts=stereo[p{k}]")
            labels.append(f"[p{k}]")
        fc = ";".join(chains) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"
        voice_track = work / "voice.wav"
        run(["ffmpeg", "-y", "-i", str(audio), "-filter_complex", fc, "-map", "[out]",
             str(voice_track)])

    # 3) 動画素材：全素材を番号順・均等割り
    mats = numbered(a.materials, VIDEO_EXT | IMAGE_EXT)
    n = max(1, len(mats))
    cut = total_video / n
    cuts = []
    for i in range(n):
        out = work / f"cut{i:02d}.mp4"
        make_cut(mats[i] if mats else None, cut, out)
        cuts.append(out)
    (work / "list.txt").write_text("".join(f"file '{c}'\n" for c in cuts))
    base = work / "base.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "list.txt"),
         "-c", "copy", str(base)])

    # 4) テロップ（画面中央）＋ PR 表示
    inputs, overlays, prev = ["-i", str(base)], [], "0:v"
    pr = work / "pr.png"; pr_badge(pr)
    inputs += ["-i", str(pr)]
    overlays.append(f"[{prev}][1:v]overlay=0:0[v1]"); prev = "v1"
    for i, u in enumerate(timeline):
        png = work / f"t{i:03d}.png"
        telop_png(u["text"], u["color"], a.font_size, png)
        inputs += ["-i", str(png)]
        nxt = f"v{i+2}"
        overlays.append(f"[{prev}][{i+2}:v]overlay=0:0:enable='between(t,{u['start']},{u['end']})'[{nxt}]")
        prev = nxt
    ai = len(timeline) + 2

    # 5) BGM（メイン音声の15%でループ）
    bgms = numbered(a.bgm, AUDIO_EXT) or sorted(p for p in Path(a.bgm).glob("*")
                                                   if p.suffix.lower() in AUDIO_EXT)
    afilt, amap = [], []
    if voice_track:
        inputs += ["-i", str(voice_track)]; vi = ai; ai += 1
    if bgms:
        inputs += ["-stream_loop", "-1", "-i", str(bgms[0])]; bi = ai
    if voice_track and bgms:
        afilt.append(f"[{bi}:a]volume={a.bgm_volume}[bg];[{vi}:a][bg]amix=inputs=2:duration=first:normalize=0[aout]")
        amap = ["-map", "[aout]"]
    elif voice_track:
        amap = ["-map", f"{vi}:a"]
    elif bgms:
        afilt.append(f"[{bi}:a]volume={a.bgm_volume}[aout]"); amap = ["-map", "[aout]"]

    fc = ";".join(overlays + afilt)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", *inputs, "-filter_complex", fc, "-map", f"[{prev}]", *amap,
         "-t", f"{total_video:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", a.out])

    Path(a.out).with_suffix(".telop.json").write_text(
        json.dumps({"duration": round(total_video, 2), "materials": [str(m) for m in mats],
                    "bgm": str(bgms[0]) if bgms else None, "audio": str(audio) if audio else None,
                    "telops": timeline}, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    print(f"done: {a.out} ({total_video:.1f}s, cuts={n}, telops={len(timeline)}, "
          f"voice={'yes' if voice_track else 'no'}, bgm={'yes' if bgms else 'no'})")


if __name__ == "__main__":
    main()
