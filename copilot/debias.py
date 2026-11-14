"""Choose the letter without the model's letter bias.

Three tools, all cheap in time:

1. Log-probabilities. Instead of the greedy letter we read the model's score for A..E.
2. Prior calibration (contextual calibration, Zhao et al. 2021). calibrate.py measures how
   much probability the model gives each letter on average. At answer time we subtract
   alpha * log(prior), so a model that loves D has to be *more* sure to answer D.
3. Option-order averaging. With passes > 1 we ask again with the options cyclically shifted
   (the letters stay A..E, the values move) and average the scores per original option.
   Position bias cancels. Each extra pass costs about one more reading of the options.

Standard library only.
"""
import json
import math
import os
import time

from .config import CONFIG, ROOT
from .llm import URL, letter_logprobs

# Committed on purpose: it holds only averages, no M3Kang text, and the jury's run needs it.
CALIB_PATH = os.path.join(ROOT, "copilot", "calibration.json")
FLOOR = -20.0                  # log-probability for a letter the server did not report
SHIFTS = (0, 2, 4, 1, 3)       # order of the cyclic shifts; the first passes are the most spread out


def setting(name, default):
    """Environment variable first, then config.env, then the default. Lets you run
    `PASSES=2 python3 eval.py --name perm2` without editing any file."""
    value = os.environ.get(name) or CONFIG.get(name)
    return default if value in (None, "") else value


def load_calibration(path=CALIB_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def letters_of(problem):
    return [l for l in "ABCDE" if l in (problem.get("options") or {})]


def shifted(problem, shift):
    """A copy of the problem with the option values moved `shift` places.

    Returns (problem_copy, mapping) where mapping[shown_letter] = original_letter.
    """
    letters = letters_of(problem)
    n = len(letters)
    options = problem["options"]
    mapping = {shown: letters[(i + shift) % n] for i, shown in enumerate(letters)}
    return {**problem, "options": {shown: options[orig] for shown, orig in mapping.items()}}, mapping


def log_softmax(scores, letters):
    """Normalise {letter: logprob} over `letters` (a missing letter gets FLOOR)."""
    values = {l: scores.get(l, FLOOR) for l in letters}
    top = max(values.values())
    z = top + math.log(sum(math.exp(v - top) for v in values.values()))
    return {l: v - z for l, v in values.items()}


def choose_letter(problem, examples=(), passes=1, prior=None, alpha=1.0, budget=50.0, url=URL):
    """Return (letter, info). The problem must have its options.

    budget is the seconds this call may use. An extra pass only starts if the slowest pass
    so far, times 1.3, still fits. If an extra pass fails, we answer with the passes we have.
    info: passes, probs (normalised, before calibration), raw_argmax, calibrated, logprobs, prompt_tokens.
    """
    letters = letters_of(problem)
    total = {l: 0.0 for l in letters}
    start = time.perf_counter()
    done, slowest, prompt_tokens, used_logprobs = 0, 0.0, 0, True
    for shift in SHIFTS[:max(1, passes)]:
        elapsed = time.perf_counter() - start
        if done and elapsed + 1.3 * slowest > budget:
            break
        shown, mapping = shifted(problem, shift)
        began = time.perf_counter()
        try:
            scores, greedy, timings = letter_logprobs(shown, examples, timeout=max(budget - elapsed, 1.0), url=url)
        except OSError:  # includes timeouts and URLError
            if done:
                break
            raise
        slowest = max(slowest, time.perf_counter() - began)
        prompt_tokens += timings.get("prompt_n", 0)
        if len(scores) < 2:  # the server sent no usable logprobs: trust its letter
            used_logprobs = False
            scores = {greedy: 0.0}
        original = {mapping[s]: v for s, v in scores.items() if s in mapping}
        for l, v in log_softmax(original, letters).items():
            total[l] += v
        done += 1
    mean = {l: total[l] / done for l in letters}
    probs = {l: math.exp(v) for l, v in log_softmax(mean, letters).items()}
    raw_argmax = min(letters, key=lambda l: (-mean[l], l))
    adjusted = dict(mean)
    if prior:
        adjusted = {l: mean[l] - alpha * math.log(max(prior.get(l, 0.2), 0.01)) for l in letters}
    letter = min(letters, key=lambda l: (-adjusted[l], l))  # ties: alphabetical, deterministic
    return letter, {"passes": done, "probs": probs, "raw_argmax": raw_argmax, "calibrated": bool(prior),
                    "logprobs": used_logprobs, "prompt_tokens": prompt_tokens}


def no_options_answer(problem, calibration, default):
    """When OCR found no options the model cannot know the answer: use the most common
    label among training problems in the same state (per level when there are enough)."""
    table = calibration.get("no_options") or {}
    return table.get("by_level", {}).get(str(problem.get("level")), table.get("overall", default))
