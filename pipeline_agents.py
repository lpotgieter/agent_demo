#!/usr/bin/env python3
"""
Multi-agent pipeline demo: one controller + one coding agent per workflow step
==============================================================================
Runs locally with Ollama + Qwen. The agents write and run their own scripts.

Project layout (created by `init`):
    demo_project/
        WORKFLOW.md          <- the controller reads this: which steps, in which order
        data/                <- input FASTQs (one deliberately low quality)
        ref/ref.fa           <- toy reference genome
        notes/               <- copy of ../notes/ (facility tool notes), given to every agent
        01_qc/TASK.md        <- agent 1's instructions (FastQC, decide pass/fail)
        02_align/TASK.md     <- agent 2's instructions (align the passing samples)

Setup:
    ollama pull qwen3:14b      # qwen3:8b is faster but gets stuck much more often
    pip install ollama
    conda install -c bioconda fastqc bwa samtools   # the tools the agents will use

Usage:
    python pipeline_agents.py init demo_project
    python pipeline_agents.py run  demo_project              # asks before every shell command
    python pipeline_agents.py run  demo_project --sandbox    # no prompts; the OS only lets each
                                                             # agent write in its own step folder
    python pipeline_agents.py run  demo_project --yes        # no prompts AND no sandbox (don't)

Sandbox: Linux uses bubblewrap (bwrap), macOS uses the built-in sandbox-exec.
Inside it, the whole machine is read-only except the agent's step folder and /tmp,
and there is no network.

Design in one sentence: the CONTROLLER is plain code (fixed order, checks outputs,
like a mini Snakemake), and the WORKERS are LLM agents (flexible inside one folder).
"""

import argparse
import gzip
import json
import os
import platform
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

import ollama

MODEL = os.environ.get("AGENT_MODEL", "qwen3:14b")
MAX_STEPS = 30                                   # per worker
# Qwen3 "thinks" before answering by default. On a laptop that can mean tens of
# thousands of hidden tokens per step. Off by default; AGENT_THINK=1 turns it on.
# Thinking is slower, but it helps most when the agent has to debug its own script.
THINK = os.environ.get("AGENT_THINK", "0") == "1"
OPTIONS = {
    # Qwen's recommended sampling. temperature 0 (greedy) made the agents rewrite the
    # same broken script again and again; Qwen warns about this.
    # The price: two runs are no longer identical.
    "temperature": 0.6 if THINK else 0.7,
    "top_p": 0.95 if THINK else 0.8,
    "top_k": 20,
    "num_ctx": 32768 if THINK else 16384,     # Ollama's default context is too small for coding
    # GUARDRAIL: cap tokens per reply, so a runaway generation can't hang the run.
    # Thinking tokens count too, so thinking needs a bigger cap.
    "num_predict": 8192 if THINK else 4096,
}


# ===========================================================================
# WORKER AGENT: one per step folder
# ===========================================================================
WORKER_PROMPT = """You are a bioinformatics coding agent working in ONE workflow step folder.
Your task (TASK.md), the project layout and the facility's tool notes are in your first message.
You may read anything in the project, but only write files inside your step folder.
Write your work as a script (run.sh or run.py) and run it, so it is reproducible.
Start bash scripts with `set -euo pipefail`, so any failing command stops the script.
Always check command output. If something fails, read the error, fix the script, retry.
When the expected output exists and looks correct, call task_complete with a short summary.
If you truly cannot continue without a human (e.g. an input file is missing), call
report_blocked with the reason instead of repeating yourself."""


def clip(text: str, limit: int) -> str:
    """Shorten long output but keep BOTH ends: usage lines and --help options are at
    the top, errors are at the bottom. Keeping only the tail hid FastQC's --outdir."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - limit} characters cut ...]\n{text[-half:]}"


def sandbox_command(command: str, step: Path) -> list[str]:
    """Wrap a shell command so the OS only allows writes inside `step` (and temp), no network."""
    home = step / ".home"                              # tools that write to ~ (e.g. Java) get a private home
    home.mkdir(exist_ok=True)
    if platform.system() == "Linux":
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise RuntimeError("bubblewrap not found: run inside `pixi run`, or apt install bubblewrap")
        return [bwrap, "--ro-bind", "/", "/",          # whole filesystem read-only...
                "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                "--bind", str(step), str(step),        # ...except the step folder
                "--unshare-net", "--die-with-parent",  # no network
                "--setenv", "HOME", str(home), "--chdir", str(step),
                "bash", "-c", command]
    if platform.system() == "Darwin":
        profile = f"""(version 1)
(allow default)
(deny network-outbound (remote ip))
(deny file-write*)
(allow file-write* (subpath "{step}") (subpath "/private/tmp")
                   (subpath "/private/var/folders") (subpath "/dev"))"""
        return ["sandbox-exec", "-p", profile, "/usr/bin/env", f"HOME={home}",
                "bash", "-c", command]
    raise RuntimeError(f"no sandbox available on {platform.system()}")


def make_tools(step: Path, project: Path, mode: str):
    """Build the tool functions for one worker. They are closures, so each worker
    is locked to its own folder. mode is 'ask', 'sandbox' or 'yes'."""
    finished = {}
    failed = {}                                       # command -> its error, for the repeat guard
    log = step / "agent_log.txt"

    def _resolve(path: str, allowed_root: Path) -> Path:
        p = (step / path).resolve()
        if not p.is_relative_to(allowed_root):
            raise ValueError(f"'{path}' is outside {allowed_root.name}/ - not allowed")
        return p

    def list_dir(path: str = ".") -> str:
        """List the contents of a directory.

        Args:
            path: Directory relative to your step folder ('.' is your folder, '..' is the project).

        Returns:
            One entry per line; directories end with '/'.
        """
        p = _resolve(path, project)
        return "\n".join(sorted(f"{x.name}/" if x.is_dir() else x.name for x in p.iterdir())) or "(empty)"

    def read_file(path: str) -> str:
        """Read a text file (first 4000 characters).

        Args:
            path: File path relative to your step folder.

        Returns:
            The file content.
        """
        p = _resolve(path, project)
        if p.suffix in (".gz", ".zip", ".bam"):
            return "Binary file: inspect it with run_command instead (e.g. zcat file | head -8)."
        return p.read_text(errors="replace")[:4000]

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file inside your step folder, e.g. a script.
        The content is written exactly as given: shell syntax such as $(...) is NOT run.
        To save a command's output, put it in a script and run that with run_command.
        To copy a file, use run_command('cp ../ref/ref.fa .'), never write_file.

        Args:
            path: File path relative to your step folder.
            content: Full file content.

        Returns:
            Confirmation.
        """
        p = _resolve(path, step)                      # writing is limited to the step folder
        p.parent.mkdir(parents=True, exist_ok=True)
        if not (p.exists() and p.read_text(errors="replace") == content):
            failed.clear()                            # a script changed, so old failures may now pass
        p.write_text(content)
        if p.suffix in (".sh", ".py"):
            p.chmod(0o755)
        return f"wrote {p.relative_to(project)} ({len(content)} characters)"

    def run_command(command: str) -> str:
        """Run a shell command with your step folder as the working directory (10 min timeout).

        Args:
            command: The shell command, e.g. 'bash run.sh'.

        Returns:
            Exit code plus the end of stdout and stderr.
        """
        print(f"      $ {command}")
        # GUARDRAIL: small models like to retry commands that already failed (or flip
        # between two failing variants). Remember every failure since the last success
        # or file write, not just the last one.
        if command.strip() in failed:
            program = command.split()[0]
            print("        (blocked: this command already failed)")
            return ("BLOCKED: you already ran exactly this command and it failed. It will fail again. "
                    f"Its error was:\n{failed[command.strip()]}\n"
                    f"Read the error. If an option is wrong, run '{program} --help' to see the real options.")
        if mode == "ask" and input("        run it? [y/N] ").strip().lower() != "y":
            # HUMAN IN THE LOOP: the operator can steer the agent, not only stop it
            hint = input("        hint for the agent (Enter to skip): ").strip()
            with log.open("a") as f:                  # keep hints: the useful ones belong in notes/
                f.write(f"$ {command}\n# DENIED by operator, hint: {hint or '(none)'}\n")
            if hint:
                return f"DENIED by the human operator, with this hint: {hint}"
            return "DENIED by the human operator. Explain why it is needed or try another approach."
        try:
            if mode == "sandbox":
                r = subprocess.run(sandbox_command(command, step), cwd=step,
                                   capture_output=True, text=True, timeout=600)
            else:
                r = subprocess.run(command, shell=True, cwd=step, capture_output=True,
                                   text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return "ERROR: command timed out after 600 seconds"
        with log.open("a") as f:                      # audit trail of everything the agent ran
            f.write(f"$ {command}\n# exit code {r.returncode}\n")
        if r.returncode != 0:
            failed[command.strip()] = clip(r.stderr, 1000)
        else:
            failed.clear()                            # something changed (e.g. mkdir): retries are fair again
        # Linux (bwrap) says "Read-only file system", macOS (sandbox-exec) "Operation not permitted"
        if mode == "sandbox" and ("Read-only file system" in r.stderr
                                  or "Operation not permitted" in r.stderr):
            r.stderr += ("\nNOTE: you may only write inside your own step folder. "
                         "Write outputs there (relative paths), never elsewhere.")
        return (f"exit code {r.returncode}\n--- stdout ---\n{clip(r.stdout, 3000)}"
                f"\n--- stderr ---\n{clip(r.stderr, 2000)}")

    def task_complete(summary: str) -> str:
        """Call this ONLY when the task in TASK.md is done and its output file exists.

        Args:
            summary: Two or three sentences on what you did and the result.

        Returns:
            Acknowledgement.
        """
        finished["summary"] = summary
        return "acknowledged"

    def report_blocked(reason: str) -> str:
        """Stop and hand over to a human, when you cannot finish the task yourself.

        Args:
            reason: What is blocking you and what you already tried.

        Returns:
            Acknowledgement.
        """
        finished["blocked"] = reason
        return "acknowledged"

    return [list_dir, read_file, write_file, run_command, task_complete, report_blocked], finished


def run_worker(step: Path, project: Path, expected: str, mode: str) -> str:
    tools, finished = make_tools(step, project, mode)
    table = {f.__name__: f for f in tools}
    writes_seen = {}                                  # (path, content) -> how often it was written
    # CONTEXT: the controller puts everything the agent must know in its first message.
    # Asking the model to "go and read" files did not work: it skipped notes/, and with
    # the notes in front of it, it skipped TASK.md. The task goes LAST, closest to the work.
    notes = "\n\n".join(n.read_text() for n in sorted((project / "notes").glob("*.md")))
    layout = "  ".join(f"{d.name}/" for d in sorted(project.iterdir())
                       if d.is_dir() and not d.name.startswith("."))
    messages = [
        {"role": "system", "content": WORKER_PROMPT},
        {"role": "user", "content": f"Facility notes on the tools (for reference):\n\n{notes}\n\n"
                                    f"Project folders (all next to each other): {layout}\n"
                                    f"Your step folder is {step.name}/; refer to the others as ../name/.\n\n"
                                    f"YOUR TASK ({step.name}/TASK.md):\n\n{(step / 'TASK.md').read_text()}\n"
                                    f"The controller expects the file {expected} in your folder "
                                    "when you are done."},
    ]

    for i in range(1, MAX_STEPS + 1):
        print(f"  [{step.name} #{i}] model is working...", end="", flush=True)
        t0 = time.time()
        response = ollama.chat(model=MODEL, messages=messages, tools=tools,
                               options=OPTIONS, think=THINK)
        msg = response.message
        print(f" {time.time() - t0:.0f}s, {response.eval_count or 0} tokens")
        messages.append(msg)

        if response.done_reason == "length":         # hit num_predict: tell the model, don't hang
            print(f"  [{step.name} #{i}] (reply cut off at {OPTIONS['num_predict']} tokens)")
            messages.append({"role": "user", "content":
                             "Your reply was cut off because it was too long. "
                             "Be brief: make one tool call at a time."})
            continue

        if not msg.tool_calls:                        # small models sometimes just talk: nudge them
            print(f"  [{step.name} #{i}] (no tool call) {(msg.content or '').strip()[:150]!r}")
            messages.append({"role": "user", "content":
                             "Keep working with the tools. Call task_complete when you are done."})
            continue

        for call in msg.tool_calls:
            name, args = call.function.name, dict(call.function.arguments or {})
            shown = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in args.items()}
            print(f"  [{step.name} #{i}] {name}({shown})")
            # GUARDRAIL: writing the exact same file for the 3rd time means the agent has
            # run out of new ideas. Fail fast and hand over to a human, don't burn all steps.
            if name == "write_file":
                key = (args.get("path"), args.get("content"))
                writes_seen[key] = writes_seen.get(key, 0) + 1
                if writes_seen[key] == 3:
                    raise RuntimeError(f"{step.name}: agent is stuck (wrote the same "
                                       f"{key[0]} 3 times). See {step.name}/agent_log.txt")
            try:
                result = table[name](**args) if name in table else f"ERROR: unknown tool {name}"
            except Exception as e:
                result = f"ERROR: {e}"
            messages.append({"role": "tool", "tool_name": name, "content": str(result)})

        # VERIFY: the agent saying "done" is not enough; the controller checks the contract.
        # Agents have "satisfied" it with an EMPTY file, and with a file containing the
        # literal text "$(grep ...)", so the controller checks the content too.
        if "blocked" in finished:                     # ESCALATE: a clean stop, a human takes over
            raise RuntimeError(f"{step.name}: agent reports it is blocked: {finished['blocked']}")

        if "summary" in finished:
            problem = check_output(step / expected)
            if problem is None:
                return finished["summary"]
            finished.clear()
            messages.append({"role": "user", "content":
                             f"Not done yet: {expected} {problem}. Find out why your script "
                             "produced this and fix it, then call task_complete again."})

    raise RuntimeError(f"{step.name}: no result after {MAX_STEPS} steps")


# ===========================================================================
# CONTROLLER: plain code, reads WORKFLOW.md
# ===========================================================================
def _check_passed_samples(lines):
    for line in lines:
        if not (line.endswith(".fastq.gz") and Path(line).is_absolute() and Path(line).exists()):
            return f"has a line that is not the absolute path of an existing FASTQ: {line[:80]!r}"


def _check_alignment_summary(lines):
    if len(lines) < 2:
        return "needs a header line plus one line per sample"
    for line in lines:
        if len(line.split("\t")) != 4:
            return f"needs 4 tab-separated columns on every line, got: {line[:80]!r}"
    for line in lines[1:]:
        try:
            [float(x) for x in line.split("\t")[1:]]
        except ValueError:
            return f"needs numbers in columns 2-4, got: {line[:80]!r}"


# The contract per output file. Small, boring checks catch most agent mistakes.
CHECKS = {"passed_samples.txt": _check_passed_samples,
          "alignment_summary.tsv": _check_alignment_summary}


def check_output(out: Path):
    """Return None if the step output is acceptable, otherwise what is wrong with it."""
    if not out.exists():
        return "does not exist in your folder"
    lines = [x for x in out.read_text(errors="replace").splitlines() if x.strip()]
    if not lines:
        return "is empty. Creating an empty file does not count"
    check = CHECKS.get(out.name)
    return check(lines) if check else None


def run_workflow(project: Path, mode: str):
    if mode == "sandbox":                             # fail early and clearly if the sandbox can't start
        probe = subprocess.run(sandbox_command("true", project), capture_output=True, text=True)
        shutil.rmtree(project / ".home", ignore_errors=True)
        if probe.returncode != 0:
            raise SystemExit(f"Sandbox could not start:\n{probe.stderr}\n"
                             "Run without --sandbox to approve commands by hand instead.")
        print("Sandbox on: each agent can only write inside its own step folder, no network.")
    workflow = (project / "WORKFLOW.md").read_text()
    # lines like:  1. 01_qc -> passed_samples.txt
    steps = re.findall(r"^\s*\d+\.\s+`?([\w./-]+)`?\s*->\s*`?([\w./-]+)`?", workflow, re.M)
    print(f"Model: {MODEL}\nWorkflow: {' -> '.join(s for s, _ in steps)}")

    for folder, expected in steps:
        step = (project / folder).resolve()
        out = step / expected
        if check_output(out) is None:                 # resume: skip finished steps
            print(f"\n=== {folder}: {expected} already exists, skipping ===")
            continue
        print(f"\n=== {folder}: agent starting (contract: produce {expected}) ===")
        summary = run_worker(step, project, expected, mode)
        print(f"=== {folder} done: {summary}")

    print("\nWorkflow complete. Each step folder has the agent's scripts and agent_log.txt.")


# ===========================================================================
# Toy project
# ===========================================================================
WORKFLOW_MD = """# Workflow: QC, then alignment

The controller runs these steps in order. Format: `folder -> expected output`.
A step starts only after the previous step has produced its output.

1. 01_qc -> passed_samples.txt
2. 02_align -> alignment_summary.tsv
"""

QC_TASK = """# Step 1: Quality control

Input: every `*.fastq.gz` file in `../data/`.

1. Run FastQC on each input file. Put the reports in `fastqc/` in this folder
   (use `--extract` so you can read each `summary.txt`).
2. A sample PASSES if "Per base sequence quality" is PASS or WARN in its `summary.txt`.
3. Write `passed_samples.txt`: the absolute path of each passing FASTQ, one per line.
4. Write `qc_notes.md`: one line per sample with PASS/FAIL and the reason.

Do all of this in ONE script, `run.sh`, so the result comes from the real FastQC output:
the script itself must create `passed_samples.txt` (e.g. by reading each `summary.txt`).
Do not type the result files by hand.

Software available: fastqc.
"""

ALIGN_TASK = """# Step 2: Alignment

Input: the FASTQ files listed in `../01_qc/passed_samples.txt` (ignore all others).
Reference: `../ref/ref.fa`. Copy it into this folder before indexing it.

1. Index the reference with `bwa index`.
2. Align each sample with `bwa mem`, then sort and index it with samtools:
   `bam/<sample>.sorted.bam`.
3. Write `alignment_summary.tsv` with the columns
   sample, total_reads, mapped_reads, percent_mapped (from `samtools flagstat`).

Software available: bwa, samtools.
"""


# Facility knowledge for the agents: one Markdown file per tool, next to this script.
# When an agent gets stuck on a tool detail, add a line there. It stays offline,
# survives `reset`, and every later run (and every person) benefits.
NOTES_DIR = Path(__file__).resolve().parent / "notes"


def init_project(project: Path):
    random.seed(42)
    for d in ("data", "ref", "notes", "01_qc", "02_align"):
        (project / d).mkdir(parents=True, exist_ok=True)

    genome = "".join(random.choices("ACGT", k=20000))
    with open(project / "ref/ref.fa", "w") as f:
        f.write(">chr_demo\n" + "\n".join(genome[i:i + 60] for i in range(0, len(genome), 60)) + "\n")

    def mutate(seq):  # ~1% sequencing errors so alignment is realistic
        return "".join(random.choice("ACGT") if random.random() < 0.01 else b for b in seq)

    samples = {"sample_A": 36, "sample_B": 34, "sample_C": 12}  # mean Phred; C fails QC
    for name, q in samples.items():
        with gzip.open(project / f"data/{name}.fastq.gz", "wt") as f:
            for i in range(3000):
                start = random.randrange(len(genome) - 100)
                seq = mutate(genome[start:start + 100])
                qual = "".join(chr(33 + max(2, min(41, int(random.gauss(q, 3))))) for _ in seq)
                f.write(f"@{name}_{i}\n{seq}\n+\n{qual}\n")

    (project / "WORKFLOW.md").write_text(WORKFLOW_MD)
    (project / "01_qc/TASK.md").write_text(QC_TASK)
    (project / "02_align/TASK.md").write_text(ALIGN_TASK)
    for note in NOTES_DIR.glob("*.md"):               # copy only if missing: never overwrite edits
        if not (project / "notes" / note.name).exists():
            shutil.copy(note, project / "notes" / note.name)
    print(f"Created {project}/ (3 samples, sample_C has low quality). Next: run {project}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "run"])
    ap.add_argument("project", type=Path)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--sandbox", action="store_true",
                       help="no prompts; commands run in an OS sandbox (writes only in the step folder)")
    group.add_argument("--yes", action="store_true", help="no prompts and no sandbox (not recommended)")
    a = ap.parse_args()
    if a.command == "init":
        init_project(a.project)
    else:
        run_workflow(a.project.resolve(), "sandbox" if a.sandbox else "yes" if a.yes else "ask")
