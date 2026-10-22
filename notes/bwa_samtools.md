# bwa and samtools (facility notes)

- Index once: `bwa index ref.fa`. bwa reads `.fastq.gz` directly, no need to unzip.
- Align, sort and index in one go (create `bam/` first):
  `bwa mem ref.fa reads.fastq.gz | samtools sort -o bam/sample.sorted.bam -`
  `samtools index bam/sample.sorted.bam`
- Read counts: use `samtools flagstat -O tsv` (tab-separated: count, count, label).
  Many labels contain the word "mapped", so match the label exactly. This prints
  total, mapped and percent mapped, tab-separated:
  `samtools flagstat -O tsv bam/sample.sorted.bam | awk -F'\t' '$3 ~ /^total / {t=$1} $3 == "mapped" {m=$1} END {printf "%s\t%s\t%.2f\n", t, m, 100*m/t}'`
- Tabs in bash: use `printf '%s\t%s\n' a b`. Plain `echo "a\tb"` writes a backslash and a t.
