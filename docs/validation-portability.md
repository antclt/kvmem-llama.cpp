# Portable validation and public evidence

Validation commands accept `--gpu INDEX`, `--gpu GPU-<device-uuid>`, or a profile
from an ignored `.kvmem-local.json`. Indices are physical `nvidia-smi` indices;
they are resolved to a UUID before launching the worker. No model name selects
a physical card. Size the workload to the selected GPU's available memory.

Without an explicit selector, the tools honor `KVMEM_GPU`, then the existing
`CUDA_VISIBLE_DEVICES`, then a local `default_gpu`. A single detected card can
be selected automatically. Multiple candidates require an explicit selection;
there is no automatic switch to another card when one is busy. Disabled CUDA
masks remain disabled. An inherited numeric CUDA mask requires `PCI_BUS_ID`
ordering; otherwise supply its UUID or an explicit physical index.

For example, keep personal preferences locally:

```json
{
  "default_gpu": "small",
  "gpu_profiles": {
    "small": "GPU-<device-uuid>",
    "27b": "GPU-<other-device-uuid>"
  }
}
```

`KVMEM_LOCAL_CONFIG` can point to another private configuration file. Legacy
profile names such as `small` and `27b` have no built-in device identities;
define them locally if needed. Configuration values are never shell commands.
`source scripts/gpu.sh INDEX` uses the same Python selector and safely quoted
environment assignments as the Python validation tools.

CUDA is discovered from `CUDA_HOME`, `CUDA_PATH`, local `cuda_root`, or `nvcc`
on `PATH`. Discovered runtime directories are prepended to inherited paths.
`scripts/build-cuda.sh` also accepts `CU13` and defaults to eight build jobs;
set `NPROC` to override that count.

The isolated MTP KV comparison requires explicit worker, model, request and
output paths. It selects an available loopback port for every trial, and only
terminates the process it started:

```sh
python scripts/mtp_kv_ab.py --gpu 0 --server "$BUILD/llama-kvmem-server" --model "$MODEL" --request "$REQUEST" --output "$RESULTS"
python scripts/speed_canary.py --gpu 0 --cli "$BUILD/llama-kvmem-cli" -m "$MODEL" --output "$RESULTS/canary.json"
```

Additional MTP worker arguments use `--server-args` with a JSON string array.
Optional `--expected-prompt-tokens` checks a workload's known token count.
The long tool workload also requires `--notes PATH` rather than a private
temporary file left by a previous experiment.
