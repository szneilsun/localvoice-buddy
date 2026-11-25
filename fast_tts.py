import json
import wave

from voice_smoke import ASSET_ROOT


MODEL = ASSET_ROOT / "models/piper-zh-cn-huayan/zh_CN-huayan-medium.onnx"
CONFIG = MODEL.with_suffix(".onnx.json")
PYTHON = ASSET_ROOT / ".venv-fasttts/Scripts/python.exe"


def available():
    return all(path.is_file() for path in (MODEL, CONFIG, PYTHON))


class PiperTTS:
    def __init__(self):
        import onnxruntime as ort
        from piper.config import PiperConfig
        from piper.voice import PiperVoice

        config = PiperConfig.from_dict(json.loads(CONFIG.read_text(encoding="utf-8")))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        session = ort.InferenceSession(str(MODEL), sess_options=options, providers=["CPUExecutionProvider"])
        self.voice = PiperVoice(config=config, session=session, use_tashkeel=False)

    def synthesize(self, text, output):
        with wave.open(str(output), "wb") as target:
            target.setparams((1, 2, self.voice.config.sample_rate, 0, "NONE", "not compressed"))
            self.voice.synthesize_wav(text, target, set_wav_format=False)
            if target.tell() <= 0:
                raise ValueError("Piper produced no speech.")
