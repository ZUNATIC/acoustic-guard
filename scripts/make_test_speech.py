"""Regenerates the synthetic speech fixtures in tests/fixtures/speech.

Uses neural text-to-speech voices (edge-tts, a development-only dependency) so the pipeline
can be tested end-to-end in several languages without recording anyone. The clips contain
scripted test sentences only.

    pip install edge-tts
    python scripts/make_test_speech.py
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import edge_tts

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "speech"

# name, voice, spoken language, expected verdict (NONE / LOW+ / MEDIUM+ / HIGH+), text
CORPUS = [
    ("en_leak", "en-US-GuyNeural", "en", "HIGH+", "This is the top secret database root password. Please do not share it with anyone."),
    ("en_benign", "en-US-JennyNeural", "en", "NONE", "Let's grab lunch after the meeting and talk about the weekend plans."),
    ("en_benign_work", "en-US-AriaNeural", "en", "NONE", "Please send the meeting notes to the whole team before Friday."),
    ("en_ransom", "en-IN-PrabhatNeural", "en", "HIGH+", "My system is compromised. There is a ransomware attack on my system."),
    ("en_intent", "en-US-AriaNeural", "en", "MEDIUM+", "I'm going to quietly copy the whole client list onto my pen drive before I resign."),
    ("en_cnic", "en-IN-NeerjaNeural", "en", "HIGH+", "My CNIC number is four two one zero one, five five six seven eight nine one, two."),
    ("ur_leak", "ur-PK-AsadNeural", "ur", "HIGH+", "میرا ڈیٹا بیس کا پاس ورڈ یہ ہے، کسی کو مت بتانا۔"),
    ("ur_benign", "ur-PK-UzmaNeural", "ur", "NONE", "آج موسم بہت اچھا ہے، چلو شام کو چائے پیتے ہیں۔"),
    ("ur_benign_work", "ur-PK-AsadNeural", "ur", "NONE", "کل صبح دس بجے میٹنگ ہے، سب لوگ وقت پر آ جانا۔"),
    ("ur_intent", "ur-PK-UzmaNeural", "ur", "MEDIUM+", "کمپنی چھوڑنے سے پہلے میں یہ ساری فائلیں اپنے ذاتی ای میل پر بھیج دوں گا۔"),
    ("ur_mixed", "ur-PK-AsadNeural", "ur", "HIGH+", "یار، سرور کا پاس ورڈ بتا دو، میں ڈیٹا کاپی کر لیتا ہوں۔"),
    ("ur_hack", "ur-PK-UzmaNeural", "ur", "HIGH+", "ہمارا سسٹم ہیک ہو گیا ہے، ڈیٹا چوری ہو رہا ہے۔"),
    ("ur_bomb", "ur-PK-AsadNeural", "ur", "MEDIUM+", "دفتر کی عمارت میں بم رکھا گیا ہے۔"),
    ("ur_cnic", "ur-PK-UzmaNeural", "ur", "HIGH+", "میرا شناختی کارڈ نمبر چار دو ایک صفر ایک پانچ پانچ چھ سات آٹھ نو ایک دو ہے۔"),
    ("hi_leak", "hi-IN-MadhurNeural", "hi", "HIGH+", "सर्वर का पासवर्ड किसी को मत बताना, यह बहुत गुप्त है।"),
    ("hi_benign", "hi-IN-SwaraNeural", "hi", "NONE", "कल हम सब मिलकर क्रिकेट मैच देखने चलेंगे।"),
    ("ar_leak", "ar-SA-HamedNeural", "ar", "HIGH+", "كلمة المرور الخاصة بقاعدة البيانات سرية جدا، لا تخبر أحدا."),
    ("ar_benign", "ar-SA-ZariyahNeural", "ar", "NONE", "الطقس جميل اليوم، هل تريد أن نشرب القهوة معا؟"),
    ("fa_leak", "fa-IR-FaridNeural", "fa", "MEDIUM+", "رمز عبور سرور را به هیچ کس نگو."),
    ("fa_benign", "fa-IR-DilaraNeural", "fa", "NONE", "امروز هوا خیلی خوب است، بیا با هم چای بنوشیم."),
]


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, voice, lang, expect, text in CORPUS:
        mp3 = OUT / f"{name}.mp3"
        await edge_tts.Communicate(text, voice).save(str(mp3))
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(mp3), "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", str(OUT / f"{name}.wav")],
            check=True,
        )
        mp3.unlink()
        manifest.append({"file": f"{name}.wav", "language": lang, "expect": expect, "voice": voice, "text": text})
        print("generated", name, flush=True)
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
