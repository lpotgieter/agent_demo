"""
Minimal agent demo: a sequencing-facility QC assistant
=======================================================
Runs fully locally with Ollama + a Qwen model.

Setup (once):
    ollama pull qwen3:8b          # or qwen3.5:9b, qwen2.5:7b ... any Qwen tag with tool support
    pip install ollama

Run:
    python facility_agent_demo.py
    python facility_agent_demo.py "Which samples fail QC and why?"

The five agent principles this script shows, in order:
  1. TOOLS      - plain Python functions the model may ask to use (it never runs them itself)
  2. SYSTEM PROMPT - the agent's role, rules and goal
  3. THE LOOP   - model -> tool call -> we run it -> result goes back -> repeat
  4. STOPPING   - the model answers with no tool call (done), or we hit MAX_STEPS
  5. GUARDRAILS - input validation, sandboxed file access, errors returned instead of crashing
  6. ACTIONS    - request_resequencing changes something in the world, and the model
                  decides which samples deserve it (the rule is enforced in code, too)

After a run, look in demo_run/ for qc_report.md and resequencing_queue.tsv.
"""

import gzip
import json
import os
import random
import sys
from pathlib import Path

import ollama

MODEL = os.environ.get("AGENT_MODEL", "qwen3:8b")
DATA_DIR = Path("demo_run").resolve()  # the only folder the agent may touch
MAX_STEPS = 10                          # guardrail: never loop forever

# Facility QC thresholds (the "business rules" the agent must apply)
MIN_READS = 1000
MIN_MEAN_Q = 30


# ---------------------------------------------------------------------------
# Toy data: three small FASTQ files, one of them deliberately bad
# ---------------------------------------------------------------------------
def make_demo_data():
    random.seed(1)
    DATA_DIR.mkdir(exist_ok=True)
    samples = {  # name: (n_reads, read_length, mean_phred)
        "sample_A": (2000, 150, 36),
        "sample_B": (1500, 150, 33),
        "sample_C": (400, 150, 24),   # too few reads AND low quality
    }
    for name, (n, length, q) in samples.items():
        path = DATA_DIR / f"{name}_R1.fastq.gz"
        if path.exists():
            continue
        with gzip.open(path, "wt") as f:
            for i in range(n):
                seq = "".join(random.choices("ACGT", k=length))
                qual = "".join(chr(33 + max(2, min(41, int(random.gauss(q, 3))))) for _ in range(length))
                f.write(f"@{name}_{i}\n{seq}\n+\n{qual}\n")
    # start every demo run with an empty queue and no old report
    for leftover in ("resequencing_queue.tsv", "qc_report.md"):
        (DATA_DIR / leftover).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 1. TOOLS
# The docstrings and type hints ARE the tool descriptions the model sees,
# so they are written for the model, not just for humans.
# ---------------------------------------------------------------------------
def _safe_path(filename: str) -> Path:
    """Guardrail: only allow files inside DATA_DIR."""
    p = (DATA_DIR / filename).resolve()
    if p.parent != DATA_DIR:
        raise ValueError(f"Access denied: {filename} is outside the run folder")
    return p


def list_fastq_files() -> str:
    """List the FASTQ files available in the current sequencing run.

    Returns:
        JSON list of FASTQ file names.
    """
    return json.dumps(sorted(p.name for p in DATA_DIR.glob("*.fastq.gz")))


def fastq_stats(filename: str) -> str:
    """Compute basic statistics for one FASTQ file.

    Args:
        filename: Name of the FASTQ file, as returned by list_fastq_files.

    Returns:
        JSON with read count, mean read length, GC percent and mean Phred quality.
    """
    path = _safe_path(filename)
    n = bases = gc = qsum = 0
    with gzip.open(path, "rt") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if i % 4 == 1:
                n += 1
                bases += len(line)
                gc += line.count("G") + line.count("C")
            elif i % 4 == 3:
                qsum += sum(ord(c) - 33 for c in line)
    return json.dumps({
        "file": filename,
        "reads": n,
        "mean_length": round(bases / n, 1),
        "gc_percent": round(100 * gc / bases, 1),
        "mean_quality": round(qsum / bases, 1),
    })


def check_qc(reads: int, mean_quality: float) -> str:
    """Check a sample against the facility's QC thresholds.

    Args:
        reads: Number of reads in the sample.
        mean_quality: Mean Phred quality score of the sample.

    Returns:
        JSON with PASS or FAIL and the reasons.
    """
    reads, mean_quality = int(float(reads)), float(mean_quality)  # models sometimes send strings
    reasons = []
    if reads < MIN_READS:
        reasons.append(f"reads {reads} < {MIN_READS}")
    if mean_quality < MIN_MEAN_Q:
        reasons.append(f"mean Q {mean_quality} < {MIN_MEAN_Q}")
    return json.dumps({"status": "FAIL" if reasons else "PASS", "reasons": reasons})


def request_resequencing(sample: str, reason: str) -> str:
    """Add a sample to the facility's resequencing queue.
    Only use this for samples that FAILED QC.

    Args:
        sample: Sample name, e.g. sample_C (without the file suffix).
        reason: Short reason the sample needs resequencing.

    Returns:
        Confirmation, or a refusal if the sample does not qualify.
    """
    # HARD GUARDRAIL: the prompt *asks* the model to only queue failures;
    # here the code *enforces* it by re-checking QC itself.
    sample = sample.removesuffix("_R1.fastq.gz")
    stats = json.loads(fastq_stats(f"{sample}_R1.fastq.gz"))
    qc = json.loads(check_qc(stats["reads"], stats["mean_quality"]))
    if qc["status"] == "PASS":
        return f"REFUSED: {sample} passed QC and cannot be queued for resequencing"

    queue = _safe_path("resequencing_queue.tsv")
    if queue.exists() and any(l.split("\t")[0] == sample for l in queue.read_text().splitlines()):
        return f"{sample} is already in the resequencing queue"
    with queue.open("a") as f:
        f.write(f"{sample}\t{reason}\n")
    return f"{sample} added to resequencing queue ({queue.name})"


def write_report(markdown: str) -> str:
    """Save the final QC report for the facility manager.

    Args:
        markdown: The full report in Markdown.

    Returns:
        Path of the saved report.
    """
    path = _safe_path("qc_report.md")
    path.write_text(markdown)
    return f"Report saved to {path}"


TOOLS = {f.__name__: f for f in [list_fastq_files, fastq_stats, check_qc,
                                 request_resequencing, write_report]}


# ---------------------------------------------------------------------------
# 2. SYSTEM PROMPT
# ---------------------------------------------------------------------------
# Note: this gives the GOAL and the RULES, not the steps.
# The model has to plan the order of tool calls itself.
SYSTEM_PROMPT = """You are the QC assistant of a genomics sequencing facility.
Use the tools to inspect the run; never guess numbers you can measure.
Every sample that fails QC must be queued for resequencing with a reason.
Passing samples must not be queued. Finish by writing a short Markdown report
with write_report, then give a 2-3 sentence summary."""


# ---------------------------------------------------------------------------
# 3 + 4. THE LOOP and STOPPING
# ---------------------------------------------------------------------------
def run_agent(task: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    for step in range(1, MAX_STEPS + 1):
        response = ollama.chat(
            model=MODEL,
            messages=messages,
            tools=list(TOOLS.values()),   # ollama turns these functions into JSON schemas
            options={"temperature": 0, "num_predict": 4096},  # deterministic, and capped length
            think=os.environ.get("AGENT_THINK", "0") == "1",  # Qwen3 thinking off unless asked
        )
        msg = response.message
        messages.append(msg)              # the model's turn becomes part of the memory

        # STOP: no tool requested -> this is the final answer
        if not msg.tool_calls:
            return msg.content

        # ACT: run every tool the model asked for, then OBSERVE (feed results back)
        for call in msg.tool_calls:
            name, args = call.function.name, call.function.arguments or {}
            print(f"[step {step}] -> {name}({args})")
            try:
                if name not in TOOLS:
                    raise ValueError(f"unknown tool {name}")
                result = TOOLS[name](**args)
            except Exception as e:       # 5. GUARDRAIL: errors go back to the model, not a crash
                result = f"ERROR: {e}"
            print(f"           <- {result[:120]}")
            messages.append({"role": "tool", "tool_name": name, "content": result})

    return f"Stopped after {MAX_STEPS} steps without a final answer."


if __name__ == "__main__":
    make_demo_data()
    task = " ".join(sys.argv[1:]) or "Please QC today's sequencing run and write the report."
    print(f"Model: {MODEL}\nTask:  {task}\n")
    answer = run_agent(task)
    print("\n=== Agent answer ===\n" + answer)
