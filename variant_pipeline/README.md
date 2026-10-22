# Variant pipeline (haploid, paired-end)

A cleaned-up version of the original lab script. Same analysis: quality trimming,
bwa mem, duplicate removal, GATK HaplotypeCaller (GVCF, ploidy 1), CombineGVCFs,
GenotypeGVCFs. The difference: each step reads exactly the files the previous step
wrote, so there are no manual renaming or read-group steps in between.

## Run it on your own data

1. A sample sheet `samples.tsv` (tab-separated, one line per sequencing run):

   ```
   accession	run	r1	r2
   accA	accA_L001	raw/accA_L001_R1_001.fastq.gz	raw/accA_L001_R2_001.fastq.gz
   accB	accB_L001	raw/accB_L001_R1_001.fastq.gz	raw/accB_L001_R2_001.fastq.gz
   accB	accB_L002	raw/accB_L002_R1_001.fastq.gz	raw/accB_L002_R2_001.fastq.gz
   ```

   An accession with several runs (resequencing) is merged automatically.

2. From the folder with the sample sheet:

   ```bash
   REF=ref/core.fna THREADS=16 pixi run --manifest-path /path/to/agent_demo/pixi.toml \
       bash /path/to/agent_demo/variant_pipeline/pipeline.sh all
   ```

   Results go to `results/` (change with `OUT=...`): `vcf/raw.vcf.gz`, per-accession
   GVCFs and BAMs, and `qc/metrics.tsv` with one line of QC numbers per accession.
   The reference folder must be writable (index files are written next to it).

3. Or step by step, e.g. to redo one accession or genotype a subset:

   ```bash
   bash pipeline.sh sample accB
   bash pipeline.sh joint accA accB
   ```

Run `bash pipeline.sh` without arguments for all commands.

## What changed compared to the old script

| Old script | Now | Why |
|---|---|---|
| `fastq_quality_trimmer -t 20 -l 30` on each file | `fastp --cut_tail -W 1 -M 20 -l 30` on both mates together | fastx is not available for Apple Silicon, and it needed `-Q 33` for modern Illumina quality scores. Same rule: cut 3' bases below Q20, drop reads under 30 bp. |
| `repair.sh` (BBMap) to re-pair reads | not needed | fastp keeps the pairs in sync; if one mate is dropped, so is the other. The old `outsingle` reads were not used downstream either. |
| `bwa mem -I -P` | `bwa mem` with `-R` (read groups) | `-I` needs a value (insert-size statistics), so it swallowed `-P`. If you meant `-P` (skip mate pairing), add it back in `align()`. |
| SAM file, then `samtools view -b`, then sort | `bwa mem \| samtools fixmate -m \| samtools sort` | No large SAM files; `fixmate -m` adds the tags `markdup` needs. |
| `samtools merge ${run1} ${run2}` | `samtools merge -o merged.bam run1.bam run2.bam` | In the old form the first file is the output, so it tried to write into run 1. |
| `samtools rmdup -S` | `samtools markdup -r` | `rmdup` is deprecated. Note: `-S` treated pairs as single reads; `markdup` uses both mates' positions. |
| read groups added separately (`*rg` files) | added by `bwa mem -R` (ID = run, SM = accession) | GATK needs them to know which reads belong to which accession. |
| `samtools faidx` only | plus `gatk CreateSequenceDictionary` | GATK needs the `.dict` file too. |
| `–variant 1 –variant 2` | `--variant results/gvcf/<acc>.g.vcf.gz` for each accession | The en dashes (from Word) are not recognised by GATK. |

Check the trimming and duplicate settings against what you used in your thesis before
running it on real data: fastp and FASTX do not count bases in exactly the same way.

## Tested

On the toy data from `python variant_review_agent.py init` (5 haploid accessions, 40 kb
genome): all steps run in about 15 seconds on a laptop, and every true SNP is called with
no false positives, except in the deliberately poor-quality accession (2.8x depth).
Not yet tested on real data or on Linux.
