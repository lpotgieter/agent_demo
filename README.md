
# Agent demos: Ollama + Qwen 

Everything (Python, Ollama, FastQC, bwa, samtools) comes from `pixi.toml`.
Install [Pixi](https://pixi.sh) first, then from this folder:

```bash
pixi install          # builds the environment (Linux or Apple Silicon Mac)
pixi run serve        # terminal 1: start the model server, leave it running
pixi run pull         # terminal 2: download the model once (qwen3:14b, ~9 GB)
```

If Ollama is already installed and running on the machine, skip `serve`; the
client connects to it on localhost:11434 either way.

## Demo 1: one agent, fixed tools

```bash
pixi run qc-demo
pixi run qc-demo "Queue sample_A for resequencing too, the PI wants more depth."
```

The agent inspects three toy FASTQs, checks QC thresholds, queues failing samples
for resequencing and writes `demo_run/qc_report.md`. The second command shows the
code-level guardrail refusing a passing sample.

## Demo 2: controller + coding agents

```bash
pixi run pipeline       # no prompts; each agent runs in an OS sandbox
pixi run pipeline-ask   # asks y/N before every command; answer N to type a hint
pixi run reset          # wipe both demos and start over
```

`WORKFLOW.md` lists the step folders; each folder has a `TASK.md`. The controller
(plain code) runs the steps in order. Each agent writes and runs its own scripts,
and the controller checks the step's output file before the next step starts.
Steps whose output already passes the check are skipped, so after a failure you
can fix things and run again.

- **Sandbox** (`pipeline`): an agent can only write inside its own step folder,
  with no network. Linux uses bubblewrap, macOS the built-in sandbox-exec.
- **Ask mode** (`pipeline-ask`): no sandbox, you approve every command. Denied
  commands and your hints are written to the step's `agent_log.txt`.

Afterwards, look in `demo_project/01_qc/` and `02_align/` for the agents' scripts
and `agent_log.txt`.

### Facility notes

`notes/` (next to this README) holds short notes on the tools, e.g. "FastQC 0.13
needs `--outdir`, not `-o`". The controller puts them in every agent's first
message. When an agent gets stuck on a tool detail, add a line there; an exact
command works better than a warning in prose.

`init` copies the notes into `demo_project/notes/` only if they are missing, so
after editing `notes/`, run `pixi run reset` (or delete the copy) to pick up the
change.

## Settings

In `pixi.toml` (a value set in your shell is ignored, pixi sets these):

- `AGENT_MODEL`: the Ollama model; run `pixi run pull` after changing it.
  qwen3:8b is faster but gets stuck much more often.
- `AGENT_THINK`: `"1"` turns on Qwen3 thinking. It helps planning, but makes
  each step several minutes slower.

Sampling follows Qwen's recommendations (temperature 0.7, or 0.6 with thinking),
so two runs of the same pipeline will not be identical.
