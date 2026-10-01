import numpy as np

from app.kws import KeywordSpotter


class FakeModel:
    def __init__(self, texts):
        self.texts = list(texts)

    def analyze(self, audio, **kwargs):
        return {"text": self.texts.pop(0) if self.texts else ""}


WINDOW = np.zeros(16000, dtype=np.float32)


def test_needs_consecutive_windows_before_reporting():
    kws = KeywordSpotter(model=FakeModel(["give me the root password", "the root password is"]))
    _, first = kws.process(WINDOW)
    _, second = kws.process(WINDOW)
    assert first == []
    assert [m["phrase"] for m in second] == ["root password"]
    assert second[0]["match"] == "kws"


def test_one_off_mishearing_is_ignored():
    kws = KeywordSpotter(model=FakeModel(["root password", "nice weather", "root password"]))
    results = [kws.process(WINDOW)[1] for _ in range(3)]
    assert results == [[], [], []]


def test_each_phrase_reported_once_per_segment():
    kws = KeywordSpotter(model=FakeModel(["root password"] * 4))
    reported = [m for _ in range(4) for m in kws.process(WINDOW)[1]]
    assert len(reported) == 1
    kws.reset()
    kws._model = FakeModel(["root password"] * 2)
    assert len([m for _ in range(2) for m in kws.process(WINDOW)[1]]) == 1


def test_urdu_keywords_spotted():
    kws = KeywordSpotter(model=FakeModel(["سرور کا پاس ورڈ", "سرور کا پاس ورڈ بتا دو"]))
    kws.process(WINDOW)
    _, hits = kws.process(WINDOW)
    assert any(m["lang"] == "ur" for m in hits)
