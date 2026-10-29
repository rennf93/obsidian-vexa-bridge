"""Speaker diarization for word-timestamped meeting audio (CPU, sherpa-onnx speaker embeddings).

Calibration: ONE 55 min 2-speaker meeting, so a thin set. NeMo TitaNet small embeddings over 1.5 s windows
with a 0.75 s hop on word-covered time, average-linkage cosine cut at 0.75 (the count stayed 2 for 0.70-0.80,
0.65 gave 3 and 0.85 or more collapsed to 1), clusters under max(5% of speech, 20 s) merged into the nearest,
then a +-3 word majority vote and runs under 1 s absorbed into the neighbour. On that meeting: 2 speakers
found without being told, 95.9% word accuracy, about 56 s per audio hour at 4 threads.

Limits: not validated on other meetings, 3 or more speakers, noisy audio or overlapping speech. The plateau
around the 0.75 cut is about 0.1 wide, and a quiet speaker under 5% of the speech (or 20 s) is merged away.

Memory: average linkage keeps the condensed distance matrix of all windows (about 890 MB at 9,599 windows,
quadratic growth), so make_windows widens the hop to keep the total at or under MAX_WINDOWS (10,000). The
0.75 s hop is kept for any meeting under the cap (roughly 2 hours of speech).

Words are (start_s, end_s, text) tuples in seconds. Diarization never raises on short or empty input: with no
words, fewer than 2 windows or a single cluster every word is speaker 0.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage

SAMPLE_RATE = 16000
THRESHOLD = 0.75
SMOOTH_WORDS = 3
MIN_RUN_SECONDS = 1.0
MAX_WINDOWS = 10_000
MAX_UTTERANCE_GAP_SECONDS = 2.0
MAX_UTTERANCE_SECONDS = 40.0

Word = tuple[float, float, str]
Windows = np.ndarray  # shape (n, 2): start and end seconds
Embedder = Callable[[np.ndarray, Windows, str, int], np.ndarray]


def speech_regions(words: Sequence[Word], max_gap: float = 0.5) -> list[list[float]]:
    """Merge words closer than max_gap seconds into [start, end] regions."""
    regions: list[list[float]] = []
    for start, end, _ in words:
        if regions and start - regions[-1][1] <= max_gap:
            regions[-1][1] = max(regions[-1][1], end)
        else:
            regions.append([start, end])
    return regions


def make_windows(words: Sequence[Word], win: float = 1.5, hop: float = 0.75, min_len: float = 0.5) -> Windows:
    """Fixed windows over word-covered time; a region shorter than win is one window, tiny ones are skipped.

    The hop is widened when needed so the total stays at or under MAX_WINDOWS.
    """
    regions = speech_regions(words)
    covered = sum(end - start for start, end in regions)
    # One extra window per region for the tail window, so budget for it up front.
    hop = max(hop, (covered - win * len(regions)) / max(MAX_WINDOWS - 2 * len(regions), 1))
    out: list[tuple[float, float]] = []
    for start, end in regions:
        if end - start < min_len:
            continue
        if end - start <= win:
            out.append((start, end))
            continue
        t = start
        while t + win <= end + 1e-9:
            out.append((t, t + win))
            t += hop
        if out[-1][1] < end - 0.3:
            out.append((end - win, end))
    windows = np.array(out, dtype=float).reshape(-1, 2)
    if len(windows) > MAX_WINDOWS:  # many short speech regions get one window each and can still overshoot
        windows = windows[np.linspace(0, len(windows) - 1, MAX_WINDOWS).astype(int)]
    return windows


def embed_windows(audio: np.ndarray, windows: Windows, model_path: str, num_threads: int = 4) -> np.ndarray:
    """One speaker embedding per window with sherpa-onnx; tests replace this, it needs the model file."""
    import sherpa_onnx

    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model_path, num_threads=num_threads, provider="cpu")
    )
    embeddings = []
    for start, end in windows:
        stream = extractor.create_stream()
        stream.accept_waveform(
            sample_rate=SAMPLE_RATE, waveform=audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
        )
        stream.input_finished()
        embeddings.append(np.array(extractor.compute(stream)))
    return np.array(embeddings, dtype=np.float32)


def cluster_windows(
    embeddings: np.ndarray,
    windows: Windows,
    threshold: float = THRESHOLD,
    min_frac: float = 0.05,
    min_sec: float = 20.0,
) -> np.ndarray:
    """Cosine average-linkage cut at threshold, then clusters that are too small merged into the nearest."""
    unit = embeddings / np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-12)
    labels = fcluster(linkage(unit, method="average", metric="cosine"), threshold, "distance") - 1
    durations = windows[:, 1] - windows[:, 0]
    seconds = {int(c): float(durations[labels == c].sum()) for c in np.unique(labels)}
    centroid_sums = {c: unit[labels == c].sum(axis=0) for c in seconds}
    total = sum(seconds.values())
    floor = max(min_frac * total, min_sec) if total > 4 * min_sec else min_frac * total
    merged_into = {c: c for c in seconds}
    while len(seconds) > 1:
        smallest = min(seconds, key=lambda c: seconds[c])
        if seconds[smallest] >= floor:
            break
        direction = centroid_sums[smallest] / np.linalg.norm(centroid_sums[smallest])
        nearest = max(
            (c for c in seconds if c != smallest),
            key=lambda c: centroid_sums[c] @ direction / np.linalg.norm(centroid_sums[c]),
        )
        seconds[nearest] += seconds.pop(smallest)
        centroid_sums[nearest] = centroid_sums[nearest] + centroid_sums.pop(smallest)
        for original, current in merged_into.items():
            if current == smallest:
                merged_into[original] = nearest
    return np.array([merged_into[int(c)] for c in labels])


def words_from_windows(words: Sequence[Word], windows: Windows, window_labels: np.ndarray) -> np.ndarray:
    """Each word takes the inverse-distance-weighted vote of the windows covering its midpoint."""
    mids = np.array([(w[0] + w[1]) / 2 for w in words])
    centers = (windows[:, 0] + windows[:, 1]) / 2
    ids = np.unique(window_labels)
    index = np.searchsorted(ids, window_labels)
    longest = float((windows[:, 1] - windows[:, 0]).max())
    lows = np.searchsorted(windows[:, 0], mids - longest, "left")
    highs = np.searchsorted(windows[:, 0], mids, "right")
    out = np.zeros(len(words), dtype=int)
    for i, mid in enumerate(mids):
        covering = np.arange(lows[i], highs[i])
        covering = covering[windows[covering, 1] >= mid]
        if len(covering) == 0:
            covering = np.array([int(np.argmin(np.abs(centers - mid)))])
        votes = np.bincount(index[covering], weights=1.0 / (0.25 + np.abs(centers[covering] - mid)), minlength=len(ids))
        out[i] = ids[int(np.argmax(votes))]
    return out


def smooth_labels(
    words: Sequence[Word], labels: np.ndarray, radius: int = SMOOTH_WORDS, min_run_sec: float = MIN_RUN_SECONDS
) -> np.ndarray:
    """Majority over a +-radius word window, then runs shorter than min_run_sec absorbed into the neighbour."""
    labels = np.asarray(labels).copy()
    count = len(labels)
    if radius:
        smoothed = labels.copy()
        for i in range(count):
            values, counts = np.unique(labels[max(0, i - radius) : i + radius + 1], return_counts=True)
            smoothed[i] = values[np.argmax(counts)]
        labels = smoothed
    changed = True
    while changed and min_run_sec:
        changed = False
        i = 0
        while i < count:
            j = i
            while j + 1 < count and labels[j + 1] == labels[i]:
                j += 1
            if words[j][1] - words[i][0] < min_run_sec and (i > 0 or j < count - 1):
                neighbour = labels[i - 1] if i > 0 else labels[j + 1]
                if neighbour != labels[i]:
                    labels[i : j + 1] = neighbour
                    changed = True
            i = j + 1
    return labels


def renumber_by_first_appearance(labels: Iterable[int]) -> list[int]:
    """Speaker ids 0, 1, 2 ... in the order the speakers first talk."""
    seen: dict[int, int] = {}
    return [seen.setdefault(int(label), len(seen)) for label in labels]


def diarize(
    audio: np.ndarray,
    words: Sequence[Word],
    model_path: str,
    embed: Embedder = embed_windows,
    threshold: float = THRESHOLD,
    num_threads: int = 4,
) -> list[int]:
    """Speaker index per word (0 for the first speaker heard); all 0 when there is too little to separate."""
    alone = [0] * len(words)
    windows = make_windows(words)
    if len(windows) < 2:
        return alone
    clusters = cluster_windows(embed(audio, windows, model_path, num_threads), windows, threshold)
    if len(np.unique(clusters)) < 2:
        return alone
    labels = smooth_labels(words, words_from_windows(words, windows, clusters))
    return renumber_by_first_appearance(labels)


def build_utterances(
    words: Sequence[Word],
    speakers: Sequence[int],
    max_gap: float = MAX_UTTERANCE_GAP_SECONDS,
    max_length: float = MAX_UTTERANCE_SECONDS,
    first_label: int = 0,
) -> list[tuple[float, float, str, str]]:
    """(start, end, label, text) per run of one speaker, split at gaps over max_gap or past max_length seconds.

    Labels are `Speaker N` with N = speaker index + first_label + 1, so later sessions of a meeting can continue
    the numbering instead of reusing it.
    """
    utterances: list[tuple[float, float, str, str]] = []
    run: list[Word] = []
    run_speaker = 0

    def flush() -> None:
        text = "".join(w[2] for w in run).strip()
        if text:
            utterances.append((run[0][0], run[-1][1], f"Speaker {run_speaker + first_label + 1}", text))

    for word, speaker in zip(words, speakers, strict=True):
        if run and (speaker != run_speaker or word[0] - run[-1][1] > max_gap or word[1] - run[0][0] > max_length):
            flush()
            run = []
        if not run:
            run_speaker = speaker
        run.append(word)
    if run:
        flush()
    return utterances
