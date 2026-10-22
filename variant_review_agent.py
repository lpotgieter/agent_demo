#!/usr/bin/env python3
"""
Demo 3: the pipeline runs the scripts, the agent reviews the results
=====================================================================
Runs locally with Ollama + Qwen.

    pipeline (fixed code)   variant_pipeline/pipeline.sh: trim, map, dedup, call
    rules (fixed code)      PASS / REVIEW / FAIL from the QC numbers
    agent (Qwen)            looks at REVIEW and FAIL samples, proposes what to do,
                            chooses the accessions for joint genotyping, writes the report
    human                   approves or rejects each proposal at the end

The agent writes no commands at all: it can only call a few fixed tools, and the
tools enforce the rules (no FAIL sample in the VCF, no decision without numbers).

Usage:
    python variant_review_agent.py init variant_project   # toy data: 5 haploid accessions
    python variant_review_agent.py run  variant_project
"""

import argparse
import gzip
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import ollama

MODEL = os.environ.get("AGENT_MODEL", "qwen3:14b")
MAX_STEPS = 20
OPTIONS = {"temperature": 0.7, "top_p": 0.8, "top_k": 20,   # Qwen's recommended sampling
           "num_ctx": 16384, "num_predict": 4096}
PIPELINE = Path(__file__).resolve().parent / "variant_pipeline" / "pipeline.sh"


# ===========================================================================
# RULES: plain code. The agent can read the status, but never change it.
# ===========================================================================
def classify(m: dict) -> tuple[str, list[str]]:
    fail, review = [], []
    if m["kept_pct"] < 50:
        fail.append(f"only {m['kept_pct']}% of reads kept after trimming (< 50)")
    if m["mean_depth"] < 10:
        fail.append(f"mean depth {m['mean_depth']}x (< 10)")
    if m["mapped_pct"] < 70:
        fail.append(f"{m['mapped_pct']}% mapped (< 70)")
    elif m["mapped_pct"] < 95:
        review.append(f"{m['mapped_pct']}% mapped (< 95)")
    if m["dup_pct"] > 20:
        review.append(f"{m['dup_pct']}% duplicates (> 20)")
    if m["breadth_pct"] < 95:
        review.append(f"{m['breadth_pct']}% of the genome covered (< 95)")
    if fail:
        return "FAIL", fail + review
    return ("REVIEW", review) if review else ("PASS", [])


def read_metrics(project: Path) -> dict:
    lines = (project / "results/qc/metrics.tsv").read_text().splitlines()
    header = lines[0].split("\t")
    table = {}
    for line in lines[1:]:
        row = dict(zip(header, line.split("\t")))
        m = {k: (row[k] if k == "accession" else float(row[k])) for k in header}
        m["status"], m["reasons"] = classify(m)
        table[m["accession"]] = m
    return table


def pipeline(project: Path, *args: str):
    """Run one pipeline command, with the project folder as working directory."""
    subprocess.run(["bash", str(PIPELINE), *args], cwd=project, check=True)


# ===========================================================================
# TOOLS for the agent: fixed actions, guardrails inside
# ===========================================================================
def make_tools(project: Path, table: dict):
    state = {"proposals": {}, "joint": None, "sites": None, "report": False, "done": None}
    qc = project / "results/qc"

    def get_qc_table() -> str:
        """Show the QC numbers and the PASS / REVIEW / FAIL status of every accession.

        Returns:
            One line per accession: numbers, status, and the rules it broke.
        """
        out = ["accession | runs | read_pairs | kept% | mapped% | dup% | depth | breadth% | status | reasons"]
        for a, m in table.items():
            out.append(f"{a} | {m['runs']:.0f} | {m['read_pairs']:.0f} | {m['kept_pct']} | {m['mapped_pct']} | "
                       f"{m['dup_pct']} | {m['mean_depth']}x | {m['breadth_pct']} | {m['status']} | "
                       f"{'; '.join(m['reasons']) or '-'}")
        return "\n".join(out)

    def get_details(accession: str) -> str:
        """Show the detailed QC output of one accession (trimming, mapping, duplicates).

        Args:
            accession: The accession name, e.g. acc01.

        Returns:
            Key lines from samtools flagstat and markdup, and the sequencing runs used.
        """
        if accession not in table:
            return f"ERROR: unknown accession {accession}. Known: {', '.join(table)}"
        runs = [l.split("\t")[1] for l in (project / "samples.tsv").read_text().splitlines()[1:]
                if l.split("\t")[0] == accession]
        flag = [l.split("\t")[0] + "  " + l.split("\t")[2] for l in
                (qc / f"{accession}.flagstat.tsv").read_text().splitlines()
                if l.split("\t")[2] in ("total (QC-passed reads + QC-failed reads)", "mapped",
                                        "mapped %", "properly paired %")]
        dup = [l for l in (qc / f"{accession}.markdup.txt").read_text().splitlines()
               if l.split(":")[0] in ("READ", "DUPLICATE PAIR", "DUPLICATE TOTAL")]
        return (f"{accession}: runs {', '.join(runs)}\n"
                f"flagstat (before duplicate removal):\n  " + "\n  ".join(flag) +
                "\nsamtools markdup:\n  " + "\n  ".join(dup))

    def propose_action(accession: str, action: str, reason: str) -> str:
        """Propose what should happen with one accession. A human approves it later.

        Args:
            accession: The accession name.
            action: One of: keep (use it in the VCF), exclude (leave it out), resequence.
            reason: Why, citing the QC numbers that support it.

        Returns:
            Confirmation, or why the proposal was refused.
        """
        if accession not in table:
            return f"ERROR: unknown accession {accession}."
        if action not in ("keep", "exclude", "resequence"):
            return "ERROR: action must be keep, exclude or resequence."
        # GUARDRAIL: no judgement without evidence a human can check
        if not re.search(r"\d", reason):
            return "REFUSED: cite the QC numbers your decision is based on."
        if action == "resequence" and table[accession]["status"] == "PASS":
            return f"REFUSED: {accession} passed all QC rules; resequencing it would waste money."
        if action == "keep" and table[accession]["status"] == "FAIL":
            return f"REFUSED: {accession} FAILED a hard QC rule and cannot be kept in the VCF."
        state["proposals"][accession] = (action, reason)
        return f"recorded: {accession} -> {action}"

    def run_joint_genotyping(accessions: str) -> str:
        """Run joint genotyping (GATK CombineGVCFs + GenotypeGVCFs) on the chosen accessions.

        Args:
            accessions: Comma-separated accession names, e.g. "acc01,acc02".

        Returns:
            Number of variant sites, and how many non-reference calls each accession has.
        """
        chosen = [a.strip() for a in accessions.split(",") if a.strip()]
        unknown = [a for a in chosen if a not in table]
        if unknown or len(chosen) < 2:
            return f"ERROR: give at least 2 known accessions. Unknown: {unknown}. Known: {', '.join(table)}"
        # GUARDRAILS: FAIL never goes in; REVIEW only after a decision was recorded
        failed = [a for a in chosen if table[a]["status"] == "FAIL"]
        if failed:
            return f"REFUSED: {', '.join(failed)} FAILED QC and cannot go into the VCF."
        undecided = [a for a in chosen if table[a]["status"] == "REVIEW"
                     and state["proposals"].get(a, ("",))[0] != "keep"]
        if undecided:
            return (f"REFUSED: {', '.join(undecided)} need review first. Look at the details and "
                    "call propose_action(..., 'keep', reason) if they should be included.")
        print(f"      (running GATK joint genotyping on {', '.join(chosen)} ...)")
        pipeline(project, "joint", *chosen)
        state["joint"] = chosen
        summary, state["sites"] = summarize_vcf(project / "results/vcf/raw.vcf.gz")
        return summary

    def write_report(content: str) -> str:
        """Write report.md for the PI: per accession what was found and decided, then the variants.

        Args:
            content: The full report in Markdown.

        Returns:
            Confirmation, or what is missing.
        """
        # CHECKS: the report must be based on real results, not written ahead of them
        if state["sites"] is None:
            return "Not saved: run joint genotyping first, and use its result in the report."
        if not re.search(rf"\b{state['sites']}\b", content):
            return ("Not saved: the report must state the number of variant sites from the joint "
                    "genotyping result.")
        missing = [a for a in table if a not in content]
        if missing:                                    # every accession must be accounted for
            return f"Not saved: the report does not mention {', '.join(missing)}. Cover every accession."
        (project / "report.md").write_text(content)
        state["report"] = True
        return "saved report.md"

    def finish(summary: str) -> str:
        """Call this when proposals are recorded, joint genotyping has run and the report is written.

        Args:
            summary: One or two sentences for the human reviewer.

        Returns:
            Acknowledgement, or what is still missing.
        """
        todo = [t for t, ok in (("run_joint_genotyping", state["joint"]), ("write_report", state["report"]))
                if not ok]
        todo += [f"propose_action for {a}" for a, m in table.items()
                 if m["status"] != "PASS" and a not in state["proposals"]]
        if todo:
            return "Not finished yet. Still to do: " + "; ".join(todo)
        state["done"] = summary
        return "acknowledged"

    tools = [get_qc_table, get_details, propose_action, run_joint_genotyping, write_report, finish]
    return tools, state


def summarize_vcf(vcf: Path) -> str:
    samples, sites, nonref = [], 0, {}
    with gzip.open(vcf, "rt") as f:
        for line in f:
            if line.startswith("#CHROM"):
                samples = line.rstrip("\n").split("\t")[9:]
                nonref = {s: 0 for s in samples}
            elif not line.startswith("#"):
                sites += 1
                for s, field in zip(samples, line.rstrip("\n").split("\t")[9:]):
                    if field.split(":")[0] not in ("0", ".", "0/0", "./."):
                        nonref[s] += 1
    return (f"wrote results/vcf/raw.vcf.gz: {sites} variant sites. Non-reference calls per accession: "
            + ", ".join(f"{s} {n}" for s, n in nonref.items())), sites


# ===========================================================================
# AGENT LOOP
# ===========================================================================
SYSTEM_PROMPT = """You are the QC reviewer of a sequencing facility. The variant-calling pipeline
has already run; you cannot change it or run other commands. Fixed rules have marked each
accession PASS, REVIEW or FAIL, and you cannot change that status.

Your job:
1. Look at the QC table.
2. For every REVIEW or FAIL accession, look at its details and propose an action
   (keep, exclude or resequence). Give the reason in plain language and cite the numbers.
   Think about the likely cause, e.g. contamination, PCR duplicates, poor-quality run.
   REVIEW means "look closer", not "exclude": keep an accession if enough good data
   remains after the problem reads are removed (e.g. depth after duplicate removal).
3. Run joint genotyping on the accessions that belong in the final VCF.
4. Write report.md for the PI: a short section per accession with the key numbers and the
   decision, then the variant summary. Be factual; say what is uncertain.
5. Call finish.
Make one tool call at a time, and read each result before deciding the next step.
A human approves or rejects your proposals afterwards."""


def review(project: Path, table: dict) -> dict:
    tools, state = make_tools(project, table)
    lookup = {f.__name__: f for f in tools}
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Please review this run."}]

    for i in range(1, MAX_STEPS + 1):
        print(f"  [review #{i}] model is working...", end="", flush=True)
        t0 = time.time()
        response = ollama.chat(model=MODEL, messages=messages, tools=tools, options=OPTIONS, think=False)
        msg = response.message
        print(f" {time.time() - t0:.0f}s, {response.eval_count or 0} tokens")
        messages.append(msg)
        if not msg.tool_calls:
            print(f"  [review #{i}] (no tool call) {(msg.content or '').strip()[:150]!r}")
            messages.append({"role": "user", "content": "Keep working with the tools. Call finish when you are done."})
            continue
        for call in msg.tool_calls:
            name, args = call.function.name, dict(call.function.arguments or {})
            shown = {k: (v if k != "content" else f"<{len(v)} chars>") for k, v in args.items()}
            print(f"  [review #{i}] {name}({shown})")
            try:
                result = lookup[name](**args) if name in lookup else f"ERROR: unknown tool {name}"
            except Exception as e:
                result = f"ERROR: {e}"
            if result.startswith(("REFUSED", "Not ", "ERROR")):
                print(f"      -> {result[:200]}")
            messages.append({"role": "tool", "tool_name": name, "content": str(result)})
        if state["done"]:
            return state
    raise RuntimeError(f"review: not finished after {MAX_STEPS} steps")


# ===========================================================================
# CONTROLLER
# ===========================================================================
def run(project: Path):
    # 1. PIPELINE: deterministic, no LLM involved
    if (project / "results/qc/metrics.tsv").exists():
        print("Pipeline results exist; skipping the pipeline (delete results/ to rerun it).")
    else:
        print("Running the variant pipeline (no LLM involved) ...")
        pipeline(project, "prepare_ref")
        for acc in dict.fromkeys(l.split("\t")[0] for l in (project / "samples.tsv").read_text().splitlines()[1:]):
            pipeline(project, "sample", acc)
        pipeline(project, "metrics_table")

    # 2. RULES: deterministic
    table = read_metrics(project)
    print("\nQC status from the fixed rules:")
    for a, m in table.items():
        print(f"  {a}: {m['status']:6} {'; '.join(m['reasons'])}")

    # 3. AGENT: judgement, inside the tools' limits
    print(f"\nReview agent ({MODEL}):")
    state = review(project, table)
    print(f"\nAgent: {state['done']}")

    # 4. HUMAN: sign-off
    print("\nProposals for your approval:")
    rows = ["accession\tstatus\taction\treason\tdecision"]
    for a, (action, reason) in state["proposals"].items():
        print(f"  {a} ({table[a]['status']}): {action}. {reason}")
        if sys.stdin.isatty():
            decision = "approved" if input("    approve? [y/N] ").strip().lower() == "y" else "rejected"
        else:
            decision = "pending"
        rows.append(f"{a}\t{table[a]['status']}\t{action}\t{reason}\t{decision}")
    (project / "decisions.tsv").write_text("\n".join(rows) + "\n")
    print(f"\nJoint genotyping used: {', '.join(state['joint'])}")
    print("Written: report.md (agent), decisions.tsv (your sign-off), results/vcf/raw.vcf.gz")
    print("If you rejected a 'keep', rerun joint genotyping without it:")
    print(f"  cd {project} && bash {PIPELINE} joint <accessions>")


# ===========================================================================
# TOY DATA: haploid genome, 5 accessions, each with a realistic problem (or none)
# ===========================================================================
def revcomp(s: str) -> str:
    return s[::-1].translate(str.maketrans("ACGT", "TGCA"))


def init_project(project: Path):
    if (project / "samples.tsv").exists():
        print(f"{project}/ already has toy data; keeping it (pixi run reset to start over).")
        return
    rng = random.Random(7)
    for d in ("ref", "reads"):
        (project / d).mkdir(parents=True, exist_ok=True)
    glen, rlen = 40000, 100
    ref = "".join(rng.choices("ACGT", k=glen))
    (project / "ref/ref.fa").write_text(
        ">chr1\n" + "\n".join(ref[i:i + 60] for i in range(0, glen, 60)) + "\n")
    contaminant = "".join(rng.choices("ACGT", k=glen))     # an unrelated organism

    accessions = ["acc01", "acc02", "acc03", "acc04", "acc05"]
    shared = rng.sample(range(100, glen - 100), 30)         # SNPs all accessions share
    truth = []
    genomes = {}
    for acc in accessions:
        g = list(ref)
        private = rng.sample(range(100, glen - 100), 8)
        for pos in shared + private:
            g[pos] = rng.choice([b for b in "ACGT" if b != ref[pos]])
            truth.append(f"{acc}\t{pos + 1}\t{ref[pos]}\t{g[pos]}")
        genomes[acc] = "".join(g)
    (project / "truth.tsv").write_text("accession\tpos\tref\talt\n" + "\n".join(truth) + "\n")

    def quality(poor: bool) -> list[int]:
        if poor:                                            # quality crashes along the read
            return [max(2, min(41, int(rng.gauss(30 - 0.5 * i, 4)))) for i in range(rlen)]
        return [max(2, min(41, int(rng.gauss(37 - 0.04 * i, 2)))) for i in range(rlen)]

    def read(seq: str, qual: list[int]) -> str:             # sequencing errors follow the quality
        return "".join(rng.choice("ACGT") if rng.random() < 10 ** (-q / 10) else b
                       for b, q in zip(seq, qual))

    # run: (accession, depth, poor quality, contaminant fraction, duplicate fraction)
    runs = {"acc01_run1": ("acc01", 40, False, 0.0, 0.0),
            "acc02_run1": ("acc02", 20, False, 0.0, 0.0),   # resequenced: two runs, merged
            "acc02_run2": ("acc02", 20, False, 0.0, 0.0),
            "acc03_run1": ("acc03", 40, True, 0.0, 0.0),    # poor-quality run
            "acc04_run1": ("acc04", 40, False, 0.2, 0.0),   # 20% contamination
            "acc05_run1": ("acc05", 40, False, 0.0, 0.35)}  # PCR duplicates
    sheet = ["accession\trun\tr1\tr2"]
    for run, (acc, depth, poor, contam, dup) in runs.items():
        n_pairs = glen * depth // (2 * rlen)
        pairs = []
        for _ in range(n_pairs):
            source = contaminant if rng.random() < contam else genomes[acc]
            flen = max(2 * rlen, int(rng.gauss(300, 30)))
            start = rng.randrange(glen - flen)
            frag = source[start:start + flen]
            pairs.append((frag[:rlen], revcomp(frag[-rlen:])))
        pairs += [rng.choice(pairs) for _ in range(int(n_pairs * dup))]   # exact PCR copies
        rng.shuffle(pairs)
        r1 = project / f"reads/{run}_R1_001.fastq.gz"
        r2 = project / f"reads/{run}_R2_001.fastq.gz"
        with gzip.open(r1, "wt") as f1, gzip.open(r2, "wt") as f2:
            for i, (s1, s2) in enumerate(pairs):
                for f, s in ((f1, s1), (f2, s2)):
                    q = quality(poor)
                    f.write(f"@{run}_{i}\n{read(s, q)}\n+\n{''.join(chr(33 + x) for x in q)}\n")
        sheet.append(f"{acc}\t{run}\treads/{r1.name}\treads/{r2.name}")
    (project / "samples.tsv").write_text("\n".join(sheet) + "\n")
    print(f"Created {project}/: 5 haploid accessions (acc02 resequenced, acc03 poor quality, "
          f"acc04 contaminated, acc05 PCR duplicates). Next: run {project}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "run"])
    ap.add_argument("project", type=Path)
    a = ap.parse_args()
    if a.command == "init":
        init_project(a.project)
    else:
        run(a.project.resolve())
