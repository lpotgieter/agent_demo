# Example runs

Full terminal output of test runs, to show in class or compare with your own runs.
Demo 2 is further down.

## Demo 3: variant pipeline + review agent (22 Oct 2026, macOS, qwen3:14b)

The toy data has a known right answer: exclude or resequence acc03 (poor quality,
2.8x), keep everything else. acc04 (20% contamination) and acc05 (26% PCR duplicates)
still have 31.8x and 40x of good data, and all their SNPs are called correctly.

| Log | What it shows |
|---|---|
| [demo3_pipeline.log](demo3_pipeline.log) | The pipeline alone, no LLM: 15 seconds, and the QC table where each accession shows its built-in problem. Every true SNP is called, with no false positives (acc03: 25 of 38, because of its low depth). |
| [demo3_guardrails.log](demo3_guardrails.log) | A **scripted fake model** tries every forbidden action: a FAIL accession in the VCF, a REVIEW accession before a decision, keeping a FAIL, resequencing a PASS, a reason without numbers, a report without the variant count or with accessions missing, finishing early. Each one is refused. |
| [demo3_review1.log](demo3_review1.log) | Qwen, first version. Finished in 5 calls (2.2 min), no getting stuck, numbers cited. But it wrote all its decisions, the genotyping and the report **in one reply** (#5), before seeing any result: it **excluded every REVIEW accession** (losing two good ones) and the report has no variant count. |
| [demo3_review2.log](demo3_review2.log) | After a code check (report must state the real variant count) and two prompt lines ("REVIEW means look closer, not exclude"; one tool call at a time). 10 calls, 2.2 min, one step at a time. **Keeps acc04**, reports the correct 54 sites, but **still excludes acc05** (40x after duplicate removal) and never names contamination for acc04. |

Both Qwen runs ran without a terminal, so every proposal ended up "pending". Run
`pixi run variants` yourself to see the sign-off step, and reject the acc05 exclusion.

### Things to point out in class

- **Fixed tools turn a failing agent into a finishing one**: no stuck runs, ~2 minutes
  instead of 10–40 (compare with Demo 2 below).
- **Guardrails stop forbidden actions, not bad allowed ones.** Excluding good data is
  allowed, so only a person catches it.
- **Code can force the order of work** (the report needs the real result), a prompt only
  asks for it.
- **Policy in the prompt helped, but did not guarantee** the right call (acc04 fixed,
  acc05 not).

## Demo 2: controller + coding agents (21 Oct 2026, macOS, `--sandbox`)

Full terminal output of 13 test runs of `pipeline_agents.py`. After each run the
code was changed to fix what went wrong, so each run shows a later version of the
script. None of them completed both steps; together they show the typical ways a
small local model fails as a coding agent, and what each guardrail catches.

| Run | Model | Setup | What happened | Fix that followed |
|---|---|---|---|---|
| [01](run01.log) | qwen3:8b | temperature 0 | Alternates `fastqc … -o fastqc/` and `-o fastqc` (FastQC 0.13 needs `--outdir`) for 30 steps. The guard only blocked a repeat of the *last* failed command. | Remember every failed command. |
| [02](run02.log) | qwen3:8b | | Guard blocks repeats and the model runs `fastqc --help`, but output was cut to its last 2000 characters, so it never saw `--outdir` (it is at the top). | Keep both start and end of long output (`clip`). |
| [03](run03.log) | qwen3:8b | | Finds `--outdir`, runs `mkdir`, then the guard wrongly blocks the now-correct retry. | A successful command resets the guard. |
| [04](run04.log) | qwen3:8b | | **Contract gaming.** Calls `task_complete` before doing anything. Step 1 then "passes" with a file containing the literal text `$(grep 'PASS' …)`: non-empty, so accepted. Step 2 inherits the garbage. | Content checks per output file; `write_file` says `$(...)` is not run. |
| [05](run05.log) | qwen3:8b | | The content check rejects a `$(find …)` file 5 times; the model writes the same text again each time. | TASK.md: one `run.sh` must create the result. |
| [06](run06.log) | qwen3:8b | | Script uses `-o` again; FastQC fails inside, the script still exits 0, and the model rewrites the identical script 12 times. | `set -euo pipefail`; stop after the same file is written 3 times. |
| [07](run07.log) | qwen3:14b | | Gets `--outdir` and `set -e` right, but looks for `sample_A/summary.txt` instead of `sample_A_fastqc/`. Stuck guard stops it. Two replies hit the 4096-token cap (~400 s each). | Facility notes in `notes/`, "read them" in the prompt. |
| [08](run08.log) | qwen3:14b | notes in a folder | **Never opens the notes.** Fixes the folder name itself, but greps the wrong field order. Its summary says "all samples passed" while its own `qc_notes.md` says all FAIL. | The controller puts the notes in the first message. |
| [09](run09.log) | qwen3:14b | notes in first message | Uses the notes (`--outdir`, `awk` line). Fails on `$sample_name_fastqc` (unbound variable) and rewrites the same script. | Qwen's recommended sampling instead of temperature 0. |
| [10](run10.log) | qwen3:14b | temperature 0.7 | **Step 1 passes, correctly** (A and B pass, C fails) in 5 calls. Step 2 builds `../data/<full path>.fastq.gz` and gets stuck despite a clear error. | Try thinking mode. |
| [11](run11.log) | qwen3:14b | thinking, step 2 only | **Never reads TASK.md**: builds a script from the notes' example names, looks for the reference in `../data/`, gives up and repeats "ref.fa is missing" for 23 replies (27 min). | TASK.md and folder layout in the first message; `report_blocked` tool. |
| [12](run12.log) | qwen3:14b | thinking, step 2 only | Right reference and samples, `bam/sample_A.sorted.bam` built. `awk '/mapped/'` matches 5 lines of `flagstat`, so `bc` fails with an unhelpful error (38 min; one reply hit the 8192 cap). | Exact `flagstat -O tsv` command in the notes. |
| [13](run13.log) | qwen3:14b | no thinking, step 2 only | "Copies" the reference with `write_file('ref.fa', 'ref.fa')`, a 6-byte fake that `bwa index` accepts. Calls `report_blocked` after 9 calls, with a wrong diagnosis. | `write_file` says to copy with `cp`. |

### Things to point out in class

- **The controller, not the agent, decides when a step is done** (runs 04, 05, 08).
- **Ask in the prompt, enforce in code.** Every lasting fix was code: guards,
  checks, what goes into the context.
- **Put what the agent must know in its context.** "Go and read X" was ignored
  (runs 08, 11).
- **Exact commands beat warnings** in the notes (runs 09, 12).
- **Errors far from their cause are hard**, even with thinking (runs 12, 13).
- With temperature > 0, every run fails somewhere different (runs 10–13).
