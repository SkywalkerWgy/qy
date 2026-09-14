# CoL2Inv
## Repository Layout

```text
col2inv/
  Benchmark/
    acsl-algorithms/              # 45 TrustC (.cbs) algorithm benchmarks
  src/
    loopinvinfer.py              # main experiment entry point
    processor.py                 # loop parsing, prompting, repair logic
    prompt.py                    # prompts used by the full method
    framac.py                    # Frama-C/WP runner and log parser
    analyze_col2inv_results.py   # standalone result analysis helper
  requirement.txt
  README.md
```

## Environment

The smoke test for this artifact was run with:

```text
Python 3.9.12
Frama-C 31.0 (Gallium)
```

Install Python dependencies:

```bash
cd Col2inv
python3 -m pip install -r requirement.txt
```

Frama-C and provers are system dependencies and are not installed by pip. For detailed installation instructions, please refer to the official guide:

> [https://git.frama-c.com/pub/frama-c/blob/master/INSTALL.md](https://git.frama-c.com/pub/frama-c/blob/master/INSTALL.md)

Follow the steps in the documentation to complete the installation on your system.
Check that they are visible:

```bash
frama-c -version
why3 --version
alt-ergo --version
z3 --version
cvc5 --version
```

## Docker Reproduction

单样例的中文逐步复现说明见 [`DOCKER_REPRODUCE.md`](DOCKER_REPRODUCE.md)。

The slim multi-stage image contains the 45 TrustC benchmarks, Ubuntu's
Python 3.12 with pinned Python packages,
OCaml 4.14.2, Frama-C 31.0, Why3 1.8.1, Alt-Ergo 2.6.3, Z3 4.13.3,
CVC5 1.1.2, and Coq 8.18.0. The first build compiles the OCaml verification
stack from source and can take several minutes. Compilers and development
headers are confined to the builder stage; the runtime image uses GNU `cpp`
for the preprocessing step required by Frama-C.

Build the image:

```bash
cd Col2inv
docker build --network host --tag col2inv:qy45 .
```

Run the default single sample and persist the result on the host:

```bash
mkdir -p ResultQY45
docker run --rm \
  --env DEEPSEEK_API_KEY \
  --volume "$PWD/ResultQY45:/opt/col2inv/ResultQY45" \
  col2inv:qy45
```

Select a sample or pass normal runner options after the image name:

```bash
docker run --rm \
  --env DEEPSEEK_API_KEY \
  --volume "$PWD/ResultQY45:/opt/col2inv/ResultQY45" \
  col2inv:qy45 --file _bubble_sort.cbs
```

The Compose equivalent uses `_binary_search.cbs` by default:

```bash
DEEPSEEK_API_KEY="sk-..." docker compose run --rm col2inv
COL2INV_SAMPLE=_quick_sort.cbs DEEPSEEK_API_KEY="sk-..." \
  docker compose run --rm col2inv
```

Run the image's offline environment check without contacting a model API:

```bash
docker run --rm --entrypoint /opt/col2inv/docker/smoke_test.sh col2inv:qy45
```

API secrets are supplied only at container runtime and are not copied into
the image. The default image intentionally excludes optional local-model GPU
dependencies and model weights.

## TrustC Benchmarks

The `Benchmark/acsl-algorithms` dataset contains 45 TrustC source files. Compared
with the original artifact, their suffix is `.cbs` instead of `.c`/`.tc`, and
their function definitions carry the `_Safe` prefix. These two source-level
changes do not replace the functional verifier: CoL2Inv still uses Frama-C/WP
to verify the ACSL contracts and generated loop invariants.

The current 45-sample composition includes a result-guided reselection carried
out on 2026-08-20.  Its mapping, validation results, and selection-bias caveat
are documented in [`BENCHMARK_RESELECTION.md`](BENCHMARK_RESELECTION.md).

CoL2Inv keeps TrustC inputs, generated candidates, and verified artifacts as
`.cbs` files. Frama-C 31 does not configure GCC to preprocess the `.cbs`
extension, so the verification adapter creates a temporary `.c` compatibility
view for WP and removes it immediately after the check. The source benchmark
is never rewritten during verification.

Some algorithm contracts use established Frama-C/ACSL predicates beyond the
small core enumerated in the current TrustC manual (for example `\valid`,
`\at`, user predicates, and lemmas). They are retained to preserve the
original benchmark semantics and are handled by the Frama-C compatibility
backend.

## Model Setup

### API Models

By default, CoL2Inv uses the DeepSeek-compatible client configured in
`src/processor.py`. Set:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

The default API endpoint is `https://api.deepseek.com` and the default model
identifier is `deepseek-flash`.

### Local Models

The current version also supports Hugging Face compatible local causal language models. Install the optional packages listed in `requirement.txt`, including a machine-appropriate `torch` build, then run with `--local-model`:

```bash
python3 loopinvinfer.py \
  --local-model \
  --local-model-path /path/to/local/model \
  --local-device-map auto \
  --local-torch-dtype auto
```

If `--local-model-path` is omitted, `--model` is used as the local model path.

## Running Experiments

### One-command Runner

Run one default sample (`_binary_search.cbs`):

```bash
cd Col2inv
export DEEPSEEK_API_KEY="sk-..."
./run_col2inv.sh
```

Select another sample or pass additional CoL2Inv arguments:

```bash
./run_col2inv.sh --file _bubble_sort.cbs
./run_col2inv.sh --file _quick_sort.cbs
```

### Single File

```bash
cd Col2inv/src
python3 loopinvinfer.py \
  --dataset acsl-algorithms \
  --file _bubble_sort.cbs \
  --model deepseek-flash \
  --output ../ResultFinal/
```

### Ablation Modes

Ablation runs are controlled uniformly by `--ablation-mode`:

```bash
# Plain ablation: plain initial prompt, feedback prompts without existing loop invariants.
python3 loopinvinfer.py \
  --dataset acsl-algorithms \
  --file _bubble_sort.cbs \
  --model deepseek-flash \
  --ablation-mode plain

# Batch-feedback ablation: full initial inference, then one feedback prompt with all errors.
python3 loopinvinfer.py \
  --dataset acsl-algorithms \
  --file _bubble_sort.cbs \
  --model deepseek-flash \
  --ablation-mode batch-feedback
```

### Full Dataset

`--runAll` runs all configured datasets. To run only one dataset, loop over the selected benchmark directory:

```bash
cd Col2inv/src
mkdir -p ../Result

for path in ../Benchmark/acsl-algorithms/*.cbs; do
  fname="$(basename "$path")"
  echo "===== ${fname} ====="
  PYTHONDONTWRITEBYTECODE=1 python3 -B loopinvinfer.py \
    --dataset acsl-algorithms \
    --file "$fname" \
    --model deepseek-flash \
    --output ../Result/ \
done
```

The runner skips a sample if its result directory already exists. Remove or rename that directory before rerunning the sample.

## Result Analysis

Use the standalone analysis script. Analyze one dataset/model/output root:

```bash
cd Col2inv/src
python3 analyze_col2inv_results.py \
  --result-root ACSL=../Result/acsl-algorithms/deepseek-flash \
  --dataset-dir ACSL=../Benchmark/acsl-algorithms
```

Add `--details` to print per-sample CSV rows. 

## Command-Line Arguments

| Argument | Description |
| --- | --- |
| `--file` | Source file name, for example `_binary_search.cbs`. |
| `--dataset` | Dataset directory under `Benchmark/`, for example `acsl-algorithms`. |
| `--model` | API model identifier, or local model path when `--local-model-path` is omitted. |
| `--output` | Root directory for generated results. |
| `--proposal` | Number of bounded repair attempts before the unlimited final-repair phase; default `5`. |
| `--sample-timeout` | Per-sample wall-clock timeout in seconds; default `0` (disabled). |
| `--local-model` | Use a local Hugging Face compatible model instead of an API model. |
| `--local-model-path` | Path to the local model directory. |
| `--local-device-map` | `device_map` passed to `AutoModelForCausalLM.from_pretrained`. |
| `--local-torch-dtype` | `torch_dtype` passed to `AutoModelForCausalLM.from_pretrained`. |
| `--local-max-new-tokens` | Maximum generated tokens for local model calls. |
| `--local-eos-token-id` | Optional EOS token id for local model generation. |
| `--ablation-mode` | `none`, `plain`, or `batch-feedback`. Controls the ablation path. |
| `--runAll` | Run all configured datasets. |
