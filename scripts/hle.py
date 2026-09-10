#!/usr/bin/env python3
"""Humanity's Last Exam through ReasonProxy: frozen, paired, resumable.

Why a second runner exists
--------------------------
`benchmark.py` drives container benchmarks: a task pool, a sandbox, an agent
harness and a program verifier, one trial at a time. Terminal-Bench 4.0 tasks
declare an 8-hour agent budget and up to 32 GiB / 16 CPUs each, so a laptop can
run a handful of trials per day and no laptop can reach the sample size a
one-or-two-point effect needs. HLE is the opposite shape: 2,500 independent
single-turn questions, no sandbox, no verifier container, an LLM judge, and
answers that are graded in seconds. That is the only shape in which this
project can currently buy statistical power, so it gets its own runner rather
than being bent into the container orchestrator.

What is kept identical to the official evaluation
-------------------------------------------------
* `SYSTEM_PROMPT` and `JUDGE_PROMPT` are copied verbatim from
  `centerforaisafety/hle@main:hle_eval/`.
* No temperature is sent (the official script leaves it commented out), so
  provider defaults decide sampling, exactly as everywhere else in this repo.
* `max_completion_tokens` defaults to the official floor of 8192; below it the
  official README reports model collapse.
* The reported headline accuracy uses the official denominator: every question
  in the declared population, with an unanswered question counted wrong.

Declared deviations, recorded in every manifest
-----------------------------------------------
* The judge is not `o3-mini`; this project has no OpenAI access. It is a fixed,
  arm-blind alias declared in the manifest and identical across arms.
* The default population is the text-only subset, because the controller models
  on Workers AI are text-to-text; image questions are declared out of
  population rather than silently scored as wrong.
* Predictions and judgements are appended to JSONL per condition, so a run that
  dies at question 1,900 resumes instead of restarting.

Statistics come from `benchmark.py` (one convention per project): Wilson
intervals per arm, exact McNemar on discordant pairs for each treatment-control
contrast, and a paired bootstrap interval on the per-question difference.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark import (  # noqa: E402  - one statistics and provenance convention
    REPO,
    mcnemar_exact,
    now_stamp,
    source_digest,
    utc_now,
    wilson_ci,
)

# Verbatim from centerforaisafety/hle@main:hle_eval/run_model_predictions.py
SYSTEM_PROMPT = (
    "Your response should be in the following format:\n"
    "Explanation: {your explanation for your answer choice}\n"
    "Answer: {your chosen answer}\n"
    "Confidence: {your confidence score between 0% and 100% for your answer}"
)

# Verbatim from centerforaisafety/hle@main:hle_eval/run_judge_results.py
JUDGE_PROMPT = """Judge whether the following [response] to [question] is correct or not based on the precise and unambiguous [correct_answer] below.

[question]: {question}

[response]: {response}

Your judgement must be in the format and criteria specified below:

extracted_final_answer: The final exact answer extracted from the [response]. Put the extracted answer as 'None' if there is no exact, final answer to extract from the response.

[correct_answer]: {correct_answer}

reasoning: Explain why the extracted_final_answer is correct or incorrect based on [correct_answer], focusing only on if there are meaningful differences between [correct_answer] and the extracted_final_answer. Do not comment on any background to the problem, do not attempt to solve the problem, do not argue for any answer different than [correct_answer], focus only on whether the answers match.

correct: Answer 'yes' if extracted_final_answer matches the [correct_answer] given above, or is within a small margin of error for numerical problems. Answer 'no' otherwise, i.e. if there if there is any inconsistency, ambiguity, non-equivalency, or if the extracted answer is incorrect.

confidence: The extracted confidence score between 0|\\%| and 100|\\%| from [response]. Put 100 if there is no confidence score available."""

# The official judge relies on OpenAI structured parse. The portable equivalent
# is an explicit json_schema the proxy forwards to whichever provider serves the
# judge alias. `strict: true` in the official pydantic model is an OpenAI
# reliability switch, not a judgement field, so it is not requested here.
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "extracted_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["extracted_final_answer", "reasoning", "correct", "confidence"],
            "properties": {
                "extracted_final_answer": {"type": "string"},
                "reasoning": {"type": "string"},
                "correct": {"type": "string", "enum": ["yes", "no"]},
                "confidence": {"type": "integer"},
            },
        },
    },
}

HLE_PARQUET = Path.home() / ".cache/reasonproxy/hle/test-00000-of-00001.parquet"
HLE_SOURCE_URL = (
    "https://huggingface.co/datasets/cais/hle/resolve/main/data/test-00000-of-00001.parquet"
)
JOB_ROOT = REPO / "runs/hle"
OFFICIAL_MIN_COMPLETION_TOKENS = 8192


# --------------------------------------------------------------------------
# dataset
# --------------------------------------------------------------------------


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def rel(path: Path) -> str:
    """Repo-relative for artifacts that live here, absolute for everything else.

    A snapshot, roster or manifest may legitimately sit outside the checkout
    (a shared dataset cache, a scratch manifest), and provenance must record
    where it actually was rather than crash on the prettier form.
    """
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def cmd_fetch(args: argparse.Namespace) -> int:
    """Download the gated HLE parquet once, into the shared task cache."""
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        raise SystemExit(
            "HLE is a gated dataset (gated=auto). Accept the terms once at "
            "https://huggingface.co/datasets/cais/hle and export HF_TOKEN."
        )
    HLE_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=600.0) as client:
        with client.stream(
            "GET", HLE_SOURCE_URL, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            if response.status_code != 200:
                body = response.read()[:200].decode("utf8", "replace")
                raise SystemExit(f"HTTP {response.status_code} from Hugging Face: {body}")
            tmp = HLE_PARQUET.with_suffix(".partial")
            written = 0
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes(1 << 20):
                    handle.write(chunk)
                    written += len(chunk)
            tmp.replace(HLE_PARQUET)
    print(f"  dataset         {HLE_PARQUET}")
    print(f"  bytes           {written:,}")
    print(f"  sha256          {sha_file(HLE_PARQUET)}")
    return 0


def load_questions(path: Path) -> list[dict[str, Any]]:
    """Every HLE row, as plain dicts, in file order."""
    import pyarrow.parquet as pq  # imported here so `analyze` needs no parquet reader

    table = pq.read_table(path)
    columns = table.column_names
    rows: list[dict[str, Any]] = []
    for record in table.to_pylist():
        rows.append({key: record.get(key) for key in columns})
    return rows


def population(rows: list[dict[str, Any]], include_images: bool) -> list[dict[str, Any]]:
    if include_images:
        return rows
    return [row for row in rows if not (row.get("image") or "").strip()]


# --------------------------------------------------------------------------
# freeze
# --------------------------------------------------------------------------


def cmd_freeze(args: argparse.Namespace) -> int:
    dataset = Path(args.dataset).expanduser() if args.dataset else HLE_PARQUET
    if not dataset.is_file():
        raise SystemExit(f"missing dataset {dataset}; run `hle.py fetch` first")
    if args.max_completion_tokens < OFFICIAL_MIN_COMPLETION_TOKENS:
        raise SystemExit(
            f"--max-completion-tokens {args.max_completion_tokens} is below the official "
            f"floor of {OFFICIAL_MIN_COMPLETION_TOKENS}; the HLE README reports model "
            "collapse below it. Raise it or declare the deviation in a fork of this check."
        )
    roster = Path(args.roster)
    if not roster.is_absolute():
        roster = REPO / roster
    if not roster.is_file():
        raise SystemExit(f"missing roster {roster}")

    rows = load_questions(dataset)
    pool = population(rows, args.include_images)
    if args.max_samples:
        pool = pool[: args.max_samples]
    ids = [row["id"] for row in pool]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate question ids in the dataset snapshot")

    conditions = list(dict.fromkeys(args.conditions))
    if len(conditions) < 2:
        raise SystemExit("declare at least one control and one treatment condition")
    if args.judge in conditions:
        raise SystemExit(
            f"judge alias {args.judge!r} is also an arm; the judge must be a separate, "
            "arm-blind alias so grading cannot vary with the condition under test"
        )

    by_type: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for row in pool:
        by_type[row.get("answer_type") or "?"] = by_type.get(row.get("answer_type") or "?", 0) + 1
        by_category[row.get("category") or "?"] = (
            by_category.get(row.get("category") or "?", 0) + 1
        )

    manifest = {
        "schema": "reasonproxy.hle/1",
        "suite": "hle",
        "frozen_utc": utc_now(),
        "dataset": {
            "source": HLE_SOURCE_URL if dataset == HLE_PARQUET else "explicit --dataset",
            "path": str(dataset),
            "sha256": sha_file(dataset),
            "rows_total": len(rows),
        },
        "population": {
            "n": len(pool),
            "include_images": bool(args.include_images),
            "rule": (
                "all rows"
                if args.include_images
                else "text-only rows (image == ''); controller models on Workers AI are "
                "text-to-text, so image questions are out of population, not wrong"
            ),
            "max_samples": args.max_samples,
            "ids_sha256": sha_text("\n".join(ids)),
            "by_answer_type": dict(sorted(by_type.items())),
            "by_category": dict(sorted(by_category.items())),
        },
        "conditions": conditions,
        "judge": {
            "condition": args.judge,
            "response_format": "json_schema (portable equivalent of the official parse)",
            "deviation": "official judge is o3-mini; no OpenAI access in this project",
        },
        "request": {
            "system_prompt_sha256": sha_text(SYSTEM_PROMPT),
            "judge_prompt_sha256": sha_text(JUDGE_PROMPT),
            "max_completion_tokens": args.max_completion_tokens,
            "temperature": None,
            "sampling_note": "no sampling parameters are sent; provider defaults decide",
        },
        "endpoint": {"proxy_url": args.proxy_url, "key_env": args.proxy_key_env},
        "roster": {"path": rel(roster), "sha256": sha_file(roster)},
        "sources": source_digest(),
        "job_root": rel(JOB_ROOT),
        "primary_metric": (
            "accuracy over the declared population, unanswered counted wrong "
            "(official denominator)"
        ),
        "contrasts": [[c, conditions[0]] for c in conditions[1:]],
        "ids": ids,
    }

    out = Path(args.out) if args.out else REPO / f"runs/frozen/hle-{now_stamp()}.json"
    if not out.is_absolute():
        out = REPO / out
    if out.exists() and not args.force:
        raise SystemExit(f"{out} exists; pass --force to overwrite")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")

    print(f"  manifest        {out}")
    print(f"  population      {len(pool)} of {len(rows)} rows")
    print(f"  conditions      {', '.join(conditions)}")
    print(f"  judge           {args.judge}")
    print(f"  completion cap  {args.max_completion_tokens}")
    print(f"  dataset sha     {manifest['dataset']['sha256'][:16]}")
    print(f"  ids sha         {manifest['population']['ids_sha256'][:16]}")
    return 0


# --------------------------------------------------------------------------
# shared request plumbing
# --------------------------------------------------------------------------


def slug(condition: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", condition.lower()).strip("-")


def condition_dir(condition: str) -> Path:
    path = JOB_ROOT / slug(condition)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    """Last record per id wins, so a retried question supersedes its failure."""
    out: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id"):
            out[row["id"]] = row
    return out


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()


def load_manifest(value: str) -> tuple[Path, dict[str, Any]]:
    path = Path(value)
    if not path.is_absolute():
        path = REPO / path
    manifest = json.loads(path.read_text())
    if manifest.get("schema") != "reasonproxy.hle/1":
        raise SystemExit(f"{path} is not a reasonproxy.hle/1 manifest")
    return path, manifest


def check_sources(manifest: dict[str, Any], allow_drift: bool) -> None:
    current = source_digest()
    if current == manifest.get("sources"):
        return
    message = "proxy/runner sources changed since freeze"
    if not allow_drift:
        raise SystemExit(f"error: {message}; re-freeze or pass --allow-source-drift")
    print(f"  warning: {message} (recorded as a deviation)", file=sys.stderr)


def questions_for(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows = {row["id"]: row for row in load_questions(Path(manifest["dataset"]["path"]))}
    missing = [qid for qid in manifest["ids"] if qid not in rows]
    if missing:
        raise SystemExit(f"{len(missing)} frozen ids are absent from the dataset snapshot")
    return [rows[qid] for qid in manifest["ids"]]


async def post_chat(
    client: httpx.AsyncClient,
    manifest: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    key = os.environ.get(manifest["endpoint"]["key_env"], "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    started = time.perf_counter()
    try:
        response = await client.post(
            manifest["endpoint"]["proxy_url"].rstrip("/") + "/chat/completions",
            json=body,
            headers=headers,
        )
    except Exception as exc:  # transport failure is a recorded outcome, not a crash
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                "latency_ms": int((time.perf_counter() - started) * 1000)}
    latency = int((time.perf_counter() - started) * 1000)
    if response.status_code != 200:
        return {
            "ok": False,
            "status": response.status_code,
            "error": response.text[:300],
            "latency_ms": latency,
        }
    try:
        payload = response.json()
    except ValueError:
        return {"ok": False, "error": "non-JSON body", "latency_ms": latency}
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "ok": True,
        "content": message.get("content"),
        "finish_reason": choice.get("finish_reason"),
        "usage": payload.get("usage") or {},
        "model": payload.get("model"),
        "latency_ms": latency,
    }


# --------------------------------------------------------------------------
# predict
# --------------------------------------------------------------------------


def user_content(question: dict[str, Any], include_images: bool) -> Any:
    text = {"type": "text", "text": question["question"]}
    image = (question.get("image") or "").strip()
    if include_images and image:
        return [text, {"type": "image_url", "image_url": {"url": image}}]
    return [text]


async def cmd_predict(args: argparse.Namespace) -> int:
    _, manifest = load_manifest(args.frozen)
    check_sources(manifest, args.allow_source_drift)
    if args.condition not in manifest["conditions"]:
        raise SystemExit(
            f"{args.condition!r} is not a frozen condition; manifest declares "
            f"{manifest['conditions']}"
        )
    questions = questions_for(manifest)
    out_path = condition_dir(args.condition) / "predictions.jsonl"
    done = read_jsonl(out_path)
    pending = [q for q in questions if not (done.get(q["id"], {}).get("ok"))]
    print(f"  condition       {args.condition}")
    print(f"  population      {len(questions)}")
    print(f"  answered        {len(questions) - len(pending)}")
    print(f"  pending         {len(pending)}")
    if not pending:
        return 0

    include_images = bool(manifest["population"]["include_images"])
    cap = manifest["request"]["max_completion_tokens"]
    limit = asyncio.Semaphore(args.workers)
    counters = {"ok": 0, "failed": 0}
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=args.timeout) as client:

        async def one(question: dict[str, Any]) -> None:
            body = {
                "model": args.condition,
                "max_completion_tokens": cap,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content(question, include_images)},
                ],
            }
            async with limit:
                result = await post_chat(client, manifest, body)
            row = {
                "id": question["id"],
                "condition": args.condition,
                "ts": utc_now(),
                "ok": bool(result.get("ok") and (result.get("content") or "").strip()),
                "response": result.get("content"),
                "finish_reason": result.get("finish_reason"),
                "usage": result.get("usage"),
                "served_model": result.get("model"),
                "latency_ms": result.get("latency_ms"),
                "error": result.get("error"),
                "status": result.get("status"),
            }
            async with lock:
                append_jsonl(out_path, row)
                counters["ok" if row["ok"] else "failed"] += 1
                seen = counters["ok"] + counters["failed"]
                if seen % args.log_every == 0 or seen == len(pending):
                    print(
                        f"  [{seen}/{len(pending)}] ok={counters['ok']} failed={counters['failed']}",
                        flush=True,
                    )

        await asyncio.gather(*(one(q) for q in pending))

    print(f"  answered        {counters['ok']}")
    print(f"  failed          {counters['failed']} (re-run this command to retry only those)")
    print(f"  predictions     {out_path}")
    return 0


# --------------------------------------------------------------------------
# judge
# --------------------------------------------------------------------------


def parse_judgement(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?|```$", "", candidate).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    correct = str(data.get("correct", "")).strip().lower()
    if correct not in ("yes", "no"):
        return None
    try:
        confidence = int(float(data.get("confidence", 100)))
    except (TypeError, ValueError):
        confidence = 100
    return {
        "extracted_final_answer": str(data.get("extracted_final_answer", ""))[:2000],
        "reasoning": str(data.get("reasoning", ""))[:2000],
        "correct": correct,
        "confidence": max(0, min(100, confidence)),
    }


async def cmd_judge(args: argparse.Namespace) -> int:
    _, manifest = load_manifest(args.frozen)
    check_sources(manifest, args.allow_source_drift)
    if args.condition not in manifest["conditions"]:
        raise SystemExit(f"{args.condition!r} is not a frozen condition")
    questions = {q["id"]: q for q in questions_for(manifest)}
    directory = condition_dir(args.condition)
    predictions = {k: v for k, v in read_jsonl(directory / "predictions.jsonl").items() if v.get("ok")}
    judged = read_jsonl(directory / "judged.jsonl")
    pending = [qid for qid in predictions if qid not in judged or not judged[qid].get("ok")]
    print(f"  condition       {args.condition}")
    print(f"  judge           {manifest['judge']['condition']}")
    print(f"  predicted       {len(predictions)}")
    print(f"  judged          {len(predictions) - len(pending)}")
    print(f"  pending         {len(pending)}")
    if not pending:
        return 0

    out_path = directory / "judged.jsonl"
    limit = asyncio.Semaphore(args.workers)
    counters = {"ok": 0, "failed": 0}
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=args.timeout) as client:

        async def one(qid: str) -> None:
            question = questions[qid]
            prompt = JUDGE_PROMPT.format(
                question=question["question"],
                correct_answer=question["answer"],
                response=predictions[qid]["response"],
            )
            body = {
                "model": manifest["judge"]["condition"],
                "max_completion_tokens": args.judge_tokens,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": JUDGE_SCHEMA,
            }
            async with limit:
                result = await post_chat(client, manifest, body)
            verdict = parse_judgement(result.get("content")) if result.get("ok") else None
            row = {
                "id": qid,
                "condition": args.condition,
                "ts": utc_now(),
                "ok": verdict is not None,
                "judge_response": verdict,
                "judge_model": result.get("model"),
                "usage": result.get("usage"),
                "latency_ms": result.get("latency_ms"),
                "error": result.get("error") if verdict is None else None,
            }
            async with lock:
                append_jsonl(out_path, row)
                counters["ok" if row["ok"] else "failed"] += 1
                seen = counters["ok"] + counters["failed"]
                if seen % args.log_every == 0 or seen == len(pending):
                    print(
                        f"  [{seen}/{len(pending)}] ok={counters['ok']} failed={counters['failed']}",
                        flush=True,
                    )

        await asyncio.gather(*(one(qid) for qid in pending))

    print(f"  judged          {counters['ok']}")
    print(f"  failed          {counters['failed']} (re-run to retry only those)")
    print(f"  judgements      {out_path}")
    return 0


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------


def calibration_error(confidences: list[float], correct: list[bool], beta: int = 100) -> float | None:
    """RMS calibration error, binned by sorted confidence (official method)."""
    if len(confidences) < beta * 2:
        return None
    order = sorted(range(len(confidences)), key=lambda i: confidences[i])
    conf = [confidences[i] for i in order]
    hit = [1.0 if correct[i] else 0.0 for i in order]
    bins = [[i * beta, (i + 1) * beta] for i in range(len(conf) // beta)]
    bins[-1] = [bins[-1][0], len(conf)]
    total = len(conf)
    acc = 0.0
    for lo, hi in bins[:-1]:
        n = hi - lo
        if n <= 0:
            continue
        diff = abs(sum(conf[lo:hi]) / n - sum(hit[lo:hi]) / n)
        acc += n / total * diff**2
    return acc**0.5


def paired_bootstrap(diffs: list[int], iters: int = 10000, seed: int = 0) -> list[float] | None:
    if not diffs:
        return None
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return [round(means[int(0.025 * iters)], 6), round(means[min(iters - 1, int(0.975 * iters))], 6)]


def cmd_analyze(args: argparse.Namespace) -> int:
    manifest_path, manifest = load_manifest(args.frozen)
    ids = manifest["ids"]
    n_population = len(ids)

    graded: dict[str, dict[str, int]] = {}
    coverage: dict[str, dict[str, int]] = {}
    confidence: dict[str, list[float]] = {}
    usage: dict[str, dict[str, int]] = {}
    for condition in manifest["conditions"]:
        directory = JOB_ROOT / slug(condition)
        predictions = {
            k: v for k, v in read_jsonl(directory / "predictions.jsonl").items() if v.get("ok")
        }
        judged = {k: v for k, v in read_jsonl(directory / "judged.jsonl").items() if v.get("ok")}
        scores: dict[str, int] = {}
        confs: list[float] = []
        tokens = {"prompt": 0, "completion": 0, "calls": 0}
        for qid in ids:
            row = judged.get(qid)
            if not row:
                continue
            verdict = row["judge_response"]
            scores[qid] = 1 if verdict["correct"] == "yes" else 0
            confs.append(verdict["confidence"] / 100)
        for qid in ids:
            got = predictions.get(qid)
            if not got:
                continue
            use = got.get("usage") or {}
            tokens["prompt"] += int(use.get("prompt_tokens") or 0)
            tokens["completion"] += int(use.get("completion_tokens") or 0)
            tokens["calls"] += 1
        graded[condition] = scores
        coverage[condition] = {
            "answered": len(predictions),
            "judged": len(scores),
            "unanswered_counted_wrong": n_population - len(scores),
        }
        confidence[condition] = confs
        usage[condition] = tokens

    report: dict[str, Any] = {
        "schema": "reasonproxy.hle-analysis/1",
        "analyzed_utc": utc_now(),
        "manifest": rel(manifest_path),
        "population": n_population,
        "primary_metric": manifest["primary_metric"],
        "arms": {},
        "contrasts": [],
        "usage": usage,
    }

    for condition, scores in graded.items():
        hits = sum(scores.values())
        official_ci = wilson_ci(hits, n_population)
        report["arms"][condition] = {
            "accuracy_official_denominator": round(hits / n_population, 6) if n_population else None,
            "passes": hits,
            "n": n_population,
            "wilson95": official_ci,
            "accuracy_judged_only": round(hits / len(scores), 6) if scores else None,
            "judged": len(scores),
            "coverage": coverage[condition],
            "calibration_error_rms": calibration_error(
                confidence[condition], [bool(scores[q]) for q in scores]
            ),
        }

    for treatment, control in manifest["contrasts"]:
        left, right = graded.get(treatment, {}), graded.get(control, {})
        both = [qid for qid in ids if qid in left and qid in right]
        b = sum(1 for qid in both if left[qid] == 1 and right[qid] == 0)
        c = sum(1 for qid in both if left[qid] == 0 and right[qid] == 1)
        diffs = [left[qid] - right[qid] for qid in both]
        delta = sum(diffs) / len(both) if both else None
        report["contrasts"].append(
            {
                "treatment": treatment,
                "control": control,
                "paired_complete_cases": len(both),
                "treatment_only_correct": b,
                "control_only_correct": c,
                "discordant": b + c,
                "discordance_rate": round((b + c) / len(both), 6) if both else None,
                "delta_paired": round(delta, 6) if delta is not None else None,
                "delta_bootstrap95": paired_bootstrap(diffs, seed=args.seed),
                "mcnemar_exact_p": round(mcnemar_exact(b, c), 6) if both else None,
                "note": (
                    "delta is computed on complete pairs only; the headline arm accuracies "
                    "use the official denominator, so the two can differ when coverage differs"
                ),
            }
        )

    out = Path(args.out) if args.out else REPO / "runs/onboarding/hle-analysis.json"
    if not out.is_absolute():
        out = REPO / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")

    print(f"population {n_population} question(s), primary metric: {manifest['primary_metric']}")
    print()
    for condition, arm in report["arms"].items():
        acc = arm["accuracy_official_denominator"]
        ci = arm["wilson95"]
        span = f"wilson95=[{ci[0]:.4f}, {ci[1]:.4f}]" if ci else ""
        print(
            f"  {condition:<24} accuracy={acc:.4f} ({arm['passes']}/{arm['n']}) {span}  "
            f"judged={arm['judged']} unanswered={arm['coverage']['unanswered_counted_wrong']}"
        )
    print()
    print("contrasts (paired, complete cases only)")
    for row in report["contrasts"]:
        print(
            f"  {row['treatment']} vs {row['control']}: delta={row['delta_paired']} "
            f"on {row['paired_complete_cases']} pairs, discordant b={row['treatment_only_correct']} "
            f"c={row['control_only_correct']}, McNemar exact p={row['mcnemar_exact_p']}, "
            f"bootstrap95={row['delta_bootstrap95']}"
        )
    print()
    print(f"analysis written: {out}")
    return 0


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Humanity's Last Exam through ReasonProxy")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="download the gated HLE parquet once")
    fetch.set_defaults(func=cmd_fetch)

    freeze = sub.add_parser("freeze", help="pin population, conditions, judge and budgets")
    freeze.add_argument("--conditions", nargs="+", required=True, help="control first, then arms")
    freeze.add_argument("--judge", required=True, help="arm-blind judge alias (a ctl/* passthrough)")
    freeze.add_argument("--roster", default="configs/research.yaml")
    freeze.add_argument(
        "--dataset", default=None, help="parquet snapshot; defaults to the fetched HLE cache"
    )
    freeze.add_argument("--proxy-url", default="http://127.0.0.1:8100/v1")
    freeze.add_argument("--proxy-key-env", default="REASONPROXY_API_KEY")
    freeze.add_argument(
        "--max-completion-tokens", type=int, default=OFFICIAL_MIN_COMPLETION_TOKENS
    )
    freeze.add_argument("--include-images", action="store_true")
    freeze.add_argument("--max-samples", type=int, default=None)
    freeze.add_argument("--out", default=None)
    freeze.add_argument("--force", action="store_true")
    freeze.set_defaults(func=cmd_freeze)

    predict = sub.add_parser("predict", help="answer the population under one condition")
    predict.add_argument("--frozen", required=True)
    predict.add_argument("--condition", required=True)
    predict.add_argument("--workers", type=int, default=8)
    predict.add_argument("--timeout", type=float, default=900.0)
    predict.add_argument("--log-every", type=int, default=25)
    predict.add_argument("--allow-source-drift", action="store_true")
    predict.set_defaults(func=cmd_predict)

    judge = sub.add_parser("judge", help="grade one condition's answers")
    judge.add_argument("--frozen", required=True)
    judge.add_argument("--condition", required=True)
    judge.add_argument("--workers", type=int, default=8)
    judge.add_argument("--timeout", type=float, default=300.0)
    judge.add_argument("--judge-tokens", type=int, default=4096)
    judge.add_argument("--log-every", type=int, default=25)
    judge.add_argument("--allow-source-drift", action="store_true")
    judge.set_defaults(func=cmd_judge)

    analyze = sub.add_parser("analyze", help="paired McNemar and Wilson report")
    analyze.add_argument("--frozen", required=True)
    analyze.add_argument("--out", default=None)
    analyze.add_argument("--seed", type=int, default=0)
    analyze.set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    result = args.func(args)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
