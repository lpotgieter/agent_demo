#!/usr/bin/env bash
# Variant calling for haploid accessions from paired-end Illumina reads.
#
# A cleaned-up version of the original lab script: the same steps, but every step
# reads exactly the files the previous step wrote, so nothing is done by hand.
#
#   trim (fastp) -> map (bwa mem, with read groups) -> merge runs -> remove duplicates
#   -> HaplotypeCaller (GVCF, ploidy 1) -> CombineGVCFs -> GenotypeGVCFs
#
# Usage:  pipeline.sh <command> [args]
#   all                  everything below, for every accession in the sample sheet
#   prepare_ref          index the reference for bwa, samtools and GATK (once)
#   sample <accession>   trim, align, dedup, call and metrics for one accession
#   trim <run>           quality-trim one sequencing run
#   align <run>          map one run, add read groups, sort
#   dedup <accession>    merge the accession's runs, remove duplicates, index
#   call <accession>     HaplotypeCaller -> GVCF
#   metrics <accession>  QC numbers for one accession -> qc/<accession>.metrics.tsv
#   metrics_table        all accessions -> qc/metrics.tsv
#   joint [acc ...]      joint genotyping -> vcf/raw.vcf.gz (default: all accessions)
#
# Settings (environment variables):
#   REF      reference FASTA                  (default: ref/ref.fa)
#   SAMPLES  sample sheet                     (default: samples.tsv)
#   OUT      output folder                    (default: results)
#   THREADS  threads for fastp/bwa/samtools   (default: 4)
#
# Sample sheet: tab-separated, with a header line, columns
#   accession  run  r1  r2
# One line per sequencing run. An accession with several runs (resequencing) gets
# its runs merged before duplicate removal. Paths are relative to where you run this.
#
# Needs: fastp, bwa, samtools, gatk4, python3 (all in pixi.toml).

set -euo pipefail

REF=${REF:-ref/ref.fa}
SAMPLES=${SAMPLES:-samples.tsv}
OUT=${OUT:-results}
THREADS=${THREADS:-4}

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }

# --- sample sheet helpers ---------------------------------------------------
accessions() { awk -F'\t' 'NR > 1 && !seen[$1]++ {print $1}' "$SAMPLES"; }
runs_of()    { awk -F'\t' -v a="$1" 'NR > 1 && $1 == a {print $2}' "$SAMPLES"; }
field()      { awk -F'\t' -v r="$1" -v c="$2" 'NR > 1 && $2 == r {print $c}' "$SAMPLES"; }  # field <run> <column>

# --- steps ------------------------------------------------------------------
prepare_ref() {
    local dict="${REF%.*}.dict"
    [ -f "$REF.bwt" ] || { log "bwa index $REF"; bwa index "$REF" 2> "$OUT/logs/bwa_index.log"; }
    [ -f "$REF.fai" ] || samtools faidx "$REF"
    # GATK needs a sequence dictionary next to the reference (samtools faidx is not enough)
    [ -f "$dict" ] || gatk CreateSequenceDictionary -R "$REF" -O "$dict" 2> "$OUT/logs/dict.log"
}

trim() {
    local run=$1
    log "trim $run"
    # Same rule as the old `fastq_quality_trimmer -t 20 -l 30`: cut 3' bases below Q20,
    # drop reads shorter than 30. fastp trims both mates together, so pairs stay in sync
    # and the old repair.sh step is no longer needed.
    fastp -i "$(field "$run" 3)" -I "$(field "$run" 4)" \
          -o "$OUT/trimmed/${run}_R1.fq.gz" -O "$OUT/trimmed/${run}_R2.fq.gz" \
          --disable_adapter_trimming --disable_quality_filtering \
          --cut_tail --cut_tail_window_size 1 --cut_tail_mean_quality 20 \
          --length_required 30 --thread "$THREADS" \
          --json "$OUT/qc/${run}.fastp.json" --html "$OUT/qc/${run}.fastp.html" \
          2> "$OUT/logs/${run}.fastp.log"
}

align() {
    local run=$1 acc
    acc=$(field "$run" 1)
    log "align $run ($acc)"
    # Read groups (ID = run, SM = accession) are what GATK uses to tell samples apart;
    # adding them here replaces the separate read-group step of the old script.
    # fixmate -m adds the mate tags that `samtools markdup` needs later.
    bwa mem -t "$THREADS" -R "@RG\tID:${run}\tSM:${acc}\tPL:ILLUMINA" "$REF" \
            "$OUT/trimmed/${run}_R1.fq.gz" "$OUT/trimmed/${run}_R2.fq.gz" 2> "$OUT/logs/${run}.bwa.log" \
        | samtools fixmate -m - - \
        | samtools sort -@ "$THREADS" -o "$OUT/bam/${run}.sorted.bam" - 2> /dev/null
}

dedup() {
    local acc=$1 input run n=0 bams=""
    for run in $(runs_of "$acc"); do
        bams="$bams $OUT/bam/${run}.sorted.bam"
        n=$((n + 1))
    done
    if [ "$n" -gt 1 ]; then                       # resequenced: merge the runs first
        log "merge $n runs of $acc"
        input="$OUT/bam/${acc}.merged.bam"
        # shellcheck disable=SC2086
        samtools merge -f -@ "$THREADS" -o "$input" $bams
    else
        input=${bams# }
    fi
    log "dedup $acc"
    samtools flagstat -O tsv "$input" > "$OUT/qc/${acc}.flagstat.tsv"     # mapping rate before dedup
    # markdup -r removes duplicates (replaces the deprecated `samtools rmdup`)
    samtools markdup -r -f "$OUT/qc/${acc}.markdup.txt" -@ "$THREADS" "$input" "$OUT/bam/${acc}.dedup.bam"
    samtools index "$OUT/bam/${acc}.dedup.bam"
    samtools coverage "$OUT/bam/${acc}.dedup.bam" > "$OUT/qc/${acc}.coverage.tsv"
}

call() {
    local acc=$1
    log "HaplotypeCaller $acc"
    gatk HaplotypeCaller -R "$REF" -I "$OUT/bam/${acc}.dedup.bam" -O "$OUT/gvcf/${acc}.g.vcf.gz" \
         -ERC GVCF -ploidy 1 2> "$OUT/logs/${acc}.haplotypecaller.log"
}

metrics() {
    local acc=$1
    # shellcheck disable=SC2046
    python3 - "$acc" "$OUT" $(runs_of "$acc") > "$OUT/qc/${acc}.metrics.tsv" <<'EOF'
import json, sys
acc, out, runs = sys.argv[1], sys.argv[2], sys.argv[3:]
raw = kept = 0
for run in runs:
    s = json.load(open(f"{out}/qc/{run}.fastp.json"))["summary"]
    raw += s["before_filtering"]["total_reads"]
    kept += s["after_filtering"]["total_reads"]
flag = {}
for line in open(f"{out}/qc/{acc}.flagstat.tsv"):
    passed, _, label = line.rstrip("\n").split("\t")
    flag[label] = passed
mapped_pct = float(flag["mapped %"].rstrip("%")) if flag["mapped %"] != "N/A" else 0.0
md = {}
for line in open(f"{out}/qc/{acc}.markdup.txt"):
    if ":" in line:
        k, v = line.split(":", 1)
        md[k.strip()] = v.strip()
dup_pct = 100 * int(md["DUPLICATE TOTAL"]) / max(int(md["READ"]), 1)
length = covered = depth_sum = 0
for line in open(f"{out}/qc/{acc}.coverage.tsv"):
    if line.startswith("#"):
        continue
    c = line.split("\t")
    n = int(c[2]) - int(c[1]) + 1
    length += n; covered += int(c[4]); depth_sum += float(c[6]) * n
print("accession\truns\tread_pairs\tkept_pct\tmapped_pct\tdup_pct\tmean_depth\tbreadth_pct")
print(f"{acc}\t{len(runs)}\t{raw // 2}\t{100 * kept / max(raw, 1):.1f}\t{mapped_pct:.1f}\t"
      f"{dup_pct:.1f}\t{depth_sum / max(length, 1):.1f}\t{100 * covered / max(length, 1):.1f}")
EOF
}

metrics_table() {
    local acc first=1
    for acc in $(accessions); do
        if [ "$first" = 1 ]; then cat "$OUT/qc/${acc}.metrics.tsv"; first=0
        else tail -n +2 "$OUT/qc/${acc}.metrics.tsv"; fi
    done > "$OUT/qc/metrics.tsv"
    log "wrote $OUT/qc/metrics.tsv"
}

joint() {
    local acc args=""
    [ $# -gt 0 ] || set -- $(accessions)             # default: every accession
    log "joint genotyping: $*"
    for acc in "$@"; do args="$args --variant $OUT/gvcf/${acc}.g.vcf.gz"; done
    # shellcheck disable=SC2086
    gatk CombineGVCFs -R "$REF" $args -O "$OUT/vcf/combined.g.vcf.gz" 2> "$OUT/logs/combinegvcfs.log"
    gatk GenotypeGVCFs -R "$REF" -V "$OUT/vcf/combined.g.vcf.gz" -O "$OUT/vcf/raw.vcf.gz" \
         2> "$OUT/logs/genotypegvcfs.log"
    log "wrote $OUT/vcf/raw.vcf.gz"
}

sample() {
    local acc=$1 run
    for run in $(runs_of "$acc"); do trim "$run"; align "$run"; done
    dedup "$acc"
    call "$acc"
    metrics "$acc"
}

all() {
    local acc
    prepare_ref
    for acc in $(accessions); do sample "$acc"; done
    metrics_table
    joint
}

# --- main -------------------------------------------------------------------
mkdir -p "$OUT"/{trimmed,bam,gvcf,vcf,qc,logs}
cmd=${1:-}
[ $# -gt 0 ] && shift
case "$cmd" in
    all|prepare_ref|sample|trim|align|dedup|call|metrics|metrics_table|joint) "$cmd" "$@" ;;
    *) sed -n '2,32p' "$0"; exit 1 ;;
esac
