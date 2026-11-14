#!/usr/bin/env python3
"""Measure the model's letter bias and save what pipeline.py needs to correct it.

    python3 calibrate.py --probe                              # 1 call: does the server give logprobs?
    python3 calibrate.py --input data/train.jsonl --n 60      # the letter prior (about 10 min)
    python3 calibrate.py --input data/train.jsonl --n 60 --passes 2
    python3 calibrate.py --no-options-only --input data/train.jsonl   # answers for problems without options (no model)
    python3 calibrate.py --rag-scores --input data/val.jsonl --n 50  # how to choose RAG_MIN_SCORE

Use TRAIN problems for the prior, never validation, or the validation score is a lie.
The result goes to copilot/calibration.json. Commit that file: it holds only averages,
and the jury's run needs it on the board.
"""
import argparse
import json
import math
import os
import random
import time
from collections import Counter, defaultdict

from copilot import debias
from copilot.client import post
from copilot.config import CONFIG
from copilot.llm import URL, build_messages, letter_logprobs

DEMO = {"id": "demo", "language": "eng", "level": 0,
        "text": "What is 2 + 3 x 4?",
        "options": {"A": "20", "B": "14", "C": "24", "D": "9", "E": "10"}, "label": "B"}


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def save(update):
    data = debias.load_calibration()
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    data["model"] = CONFIG.get("LLM_FILE", "")
    with open(debias.CALIB_PATH, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    print(f"Saved {debias.CALIB_PATH}")


def probe():
    payload = {"messages": build_messages(DEMO), "grammar": "root ::= [A-E]", "max_tokens": 1,
               "temperature": 0, "seed": int(CONFIG["SEED"]), "cache_prompt": True,
               "logprobs": True, "top_logprobs": 20}
    response = post(URL, payload, timeout=120)
    print("choices[0].logprobs as the server sends it:")
    print(json.dumps(response["choices"][0].get("logprobs"), indent=1)[:1500])
    scores, letter, timings = letter_logprobs(DEMO, timeout=120)
    print("\nGreedy letter:", letter)
    print("Letters found among the top 20:", {l: round(v, 2) for l, v in scores.items()})
    if len(scores) == 5:
        print("OK: all five letters are there.")
    elif len(scores) >= 2:
        print("Only some letters are there. Their order is still useful, but try top_logprobs=40 in llm.py.")
    else:
        print("NO usable logprobs. pipeline.py will fall back to the greedy letter (no debiasing).\n"
              "Check the raw output above: the field may have another name in this llama.cpp build.")


def letter_prior(args):
    problems = [p for p in load(args.input) if p.get("options")]
    if args.languages:
        problems = [p for p in problems if p["language"] in args.languages.split(",")]
    random.Random(7).shuffle(problems)
    problems = problems[:args.n]
    if not problems:
        raise SystemExit("No problems with options in that file.")
    print(f"{len(problems)} problems, {args.passes} pass(es) each. About "
          f"{len(problems) * args.passes * 10 / 60:.0f} minutes on the board.")
    sums = {l: 0.0 for l in "ABCDE"}
    kept, before = [], Counter()
    for n, p in enumerate(problems, 1):
        letter, info = debias.choose_letter(p, passes=args.passes, prior=None)
        for l, v in info["probs"].items():
            sums[l] += v
        kept.append((p["label"], info["probs"]))
        before[info["raw_argmax"]] += 1
        if n % 10 == 0 or n == len(problems):
            print(f"  [{n}/{len(problems)}]", flush=True)
    prior = {l: max(sums[l] / len(problems), 0.01) for l in "ABCDE"}

    after = Counter()
    hits_before = hits_after = 0
    for label, probs in kept:
        adjusted = {l: math.log(max(probs[l], 1e-9)) - args.alpha * math.log(prior[l]) for l in "ABCDE"}
        choice = min("ABCDE", key=lambda l: (-adjusted[l], l))
        after[choice] += 1
        hits_after += choice == label
        hits_before += min("ABCDE", key=lambda l: (-probs[l], l)) == label

    print("\nMean probability the model gives each letter (the prior):")
    print("  " + "  ".join(f"{l}: {prior[l]:.3f}" for l in "ABCDE") + "   (0.200 each would be no bias)")
    print("Letters chosen, before -> after correction (in the same problems):")
    print("  " + "  ".join(f"{l}: {before[l]:>2}->{after[l]:<2}" for l in "ABCDE"))
    print(f"In-sample accuracy {100 * hits_before / len(kept):.1f}% -> {100 * hits_after / len(kept):.1f}% "
          f"(optimistic: measure the real number with eval.py on validation)")
    save({"priors": {str(args.passes): {l: round(v, 4) for l, v in prior.items()}},
          "n_calibration": len(problems)})


def no_options_table(args, min_level=15):
    seen = {}
    for p in load(args.input):
        if not p.get("options") and p["id"] not in seen:
            seen[p["id"]] = p
    if not seen:
        raise SystemExit("Every problem in that file has options.")
    overall = Counter(p["label"] for p in seen.values())
    per_level = defaultdict(Counter)
    for p in seen.values():
        per_level[p["level"]][p["label"]] += 1
    table = {"overall": overall.most_common(1)[0][0],
             "by_level": {str(lv): c.most_common(1)[0][0] for lv, c in per_level.items()
                          if sum(c.values()) >= min_level},
             "counts": dict(overall), "n": len(seen)}
    total = len(seen)
    print(f"{total} distinct problems without options. Answer counts: {dict(overall)}")
    print(f"Always '{table['overall']}': {100 * overall[table['overall']] / total:.1f}% on these (in-sample)")
    print("By level:", table["by_level"] or "(not enough problems per level)")
    save({"no_options": table})


def rag_scores(args):
    from copilot.rag import Index
    index = Index(args.index)
    problems = load(args.input)
    random.Random(42).shuffle(problems)
    rows = []
    for p in problems[:args.n]:
        hits = index.search(p, k=1, exclude_id=p["id"])
        if hits:
            score, neighbour = hits[0]
            rows.append((score, neighbour["label"] == p["label"]))
    rows.sort()
    scores = [s for s, _ in rows]
    print(f"Top-1 similarity over {len(rows)} problems: min {scores[0]:.2f}  median "
          f"{scores[len(scores) // 2]:.2f}  max {scores[-1]:.2f}")
    print("threshold   share of problems that get an example   neighbour's answer = true answer")
    for t in (0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
        sel = [m for s, m in rows if s >= t]
        text = f"{100 * sum(sel) / len(sel):5.1f}% of {len(sel)}" if sel else "   -"
        print(f"  {t:.2f}      {100 * len(sel) / len(rows):5.1f}%                                  {text}")
    print("(chance for the answer to match is about 20-25%. If it never rises, RAG won't help the answer.)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", default="data/train.jsonl")
    p.add_argument("--n", type=int, default=60)
    p.add_argument("--passes", type=int, default=1)
    p.add_argument("--alpha", type=float, default=1.0)
    p.add_argument("--languages", default="")
    p.add_argument("--index", default="data/index.db")
    p.add_argument("--probe", action="store_true")
    p.add_argument("--no-options-only", action="store_true")
    p.add_argument("--rag-scores", action="store_true")
    args = p.parse_args()
    if args.probe:
        return probe()
    if args.rag_scores:
        return rag_scores(args)
    if args.no_options_only:
        return no_options_table(args)
    no_options_table(args)
    letter_prior(args)


if __name__ == "__main__":
    start = time.time()
    main()
    print(f"({time.time() - start:.0f} s)")
