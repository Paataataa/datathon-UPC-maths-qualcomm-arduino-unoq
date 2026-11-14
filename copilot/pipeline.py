"""The copilot: one problem in, one letter out.

This is the file to change first. predict.py and eval.py both call solve(),
so whatever you build here is exactly what the official run measures.

What it does now (every piece can be switched off with an environment variable or a
line in config.env, so each one is an ablation):

  route "prior"     no options found by OCR  -> most common answer in training (NO_OPTIONS_MODE=llm to undo)
  route "debiased"  options found            -> log-probabilities, option-order averaging (PASSES),
                                                prior calibration (CALIB_ALPHA)   (DEBIAS=0 to undo)
  route "plain"     the original greedy letter (an image is sent, or DEBIAS=0)
  RAG               examples are used only if their similarity >= RAG_MIN_SCORE
"""
import socket
import time
import urllib.error

from . import debias
from .config import CONFIG
from .llm import URL, answer_letter

TIME_LIMIT = float(CONFIG["TIME_LIMIT_SECONDS"])
FALLBACK = CONFIG["FALLBACK_ANSWER"]
MARGIN = 3.0  # seconds kept free for Python and for writing the answer

DEBIAS = str(debias.setting("DEBIAS", "1")) != "0"
PASSES = int(debias.setting("PASSES", 1))
ALPHA = float(debias.setting("CALIB_ALPHA", 1.0))
NO_OPTIONS_MODE = str(debias.setting("NO_OPTIONS_MODE", "prior"))   # "prior" or "llm"
RAG_MIN_SCORE = float(debias.setting("RAG_MIN_SCORE", 0.85))        # tune it with calibrate.py --rag-scores
CALIBRATION = debias.load_calibration()


def solve(problem, index=None, k=0, exclude_id=None, image_b64=None, llm_url=URL,
          time_limit=TIME_LIMIT):
    """Return {"answer", "seconds", "status", "route", ...} for one problem.

    index and k turn on retrieval: the k most similar solved problems are
    added to the prompt as examples, if they are similar enough. Each example costs reading time.
    image_b64 and llm_url send the problem image to a vision server (always the plain route).
    """
    start = time.perf_counter()
    result = {"answer": FALLBACK, "status": "ok", "prompt_tokens": 0, "retrieved": "", "examples": [],
              "route": ""}
    try:
        options = problem.get("options")
        if not options and not image_b64 and NO_OPTIONS_MODE == "prior":
            result["answer"] = debias.no_options_answer(problem, CALIBRATION, FALLBACK)
            result["route"] = "prior"
        else:
            examples = []
            if index is not None and k > 0:
                hits = [(s, p) for s, p in index.search(problem, k=k, exclude_id=exclude_id)
                        if s >= RAG_MIN_SCORE]
                examples = [p for _score, p in hits]
                result["examples"] = hits
                result["retrieved"] = ";".join(f"{p['id']}:{s:.2f}" for s, p in hits)
            remaining = time_limit - (time.perf_counter() - start) - MARGIN
            if options and not image_b64 and DEBIAS:
                prior = (CALIBRATION.get("priors") or {}).get(str(PASSES))
                letter, info = debias.choose_letter(problem, examples, PASSES, prior, ALPHA,
                                                    budget=max(remaining, 1.0), url=llm_url)
                result["prompt_tokens"] = info["prompt_tokens"]
                result["route"] = "debiased"
            else:
                letter, timings = answer_letter(problem, examples, timeout=max(remaining, 1.0),
                                                image_b64=image_b64, url=llm_url)
                result["prompt_tokens"] = timings.get("prompt_n", 0)
                result["route"] = "plain"
            result["answer"] = letter
    except (socket.timeout, TimeoutError):
        result["status"] = "timeout"
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            result["status"] = "timeout"
        else:
            result["status"] = f"error: {error.reason}"
    except (OSError, ValueError, KeyError) as error:  # a dropped connection, a malformed reply
        result["status"] = f"error: {error}"
    result["seconds"] = round(time.perf_counter() - start, 2)
    return result
