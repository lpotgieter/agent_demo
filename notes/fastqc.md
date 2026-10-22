# FastQC (facility notes)

- Our version (0.13) needs `--outdir DIR`. The short form `-o` does NOT work.
- The output folder must exist first: `mkdir -p fastqc`.
- With `--extract`, each input gets its own folder named `<sample>_fastqc`,
  where <sample> is the file name without `.fastq.gz`:
  `../data/sample_A.fastq.gz` -> `fastqc/sample_A_fastqc/summary.txt`
- `summary.txt` is tab-separated: STATUS, module name, file name. For example:
  `PASS<TAB>Per base sequence quality<TAB>sample_A.fastq.gz`
- Status of one module:
  `awk -F'\t' '$2 == "Per base sequence quality" {print $1}' fastqc/sample_A_fastqc/summary.txt`
