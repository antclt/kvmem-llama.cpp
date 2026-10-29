# NInfer multi-GPU resident Vision fix — 2026-10-29

The automatic layer split failed with `vision/patch_embedding: duplicate model
parameter declaration` when resident Vision was selected. The pinned upstream
`63428ac2` already binds and counts resident Vision in `default_stage_layers`.
`ninfer-kvmem.patch` adds a second binding after the drafter accounting. This is
an integration regression, not a required legacy behavior or a GPU capacity limit.

The trigger is more than one device, no explicit `--stage-layers`, layer pipeline
mode, and enabled resident Vision. CPU Vision skips both device-weight blocks;
an explicit layer split bypasses automatic sizing. Tensor parallelism also skips
this sizing call. The fix removes the redundant block and retains the upstream
binding, device placement and head-stage byte accounting.

## Verification

- Added a native loading regression with a synthetic eight-layer model and a
  BF16 Vision tower. It checks complete nonempty automatic stages, CPU/text-only
  equivalence, and independent byte compensation on the head device. The fixture
  must actually change the split when Vision consumes device memory, so silently
  omitting all Vision accounting cannot pass.
- The same new test against the pre-fix loading library fails with the exact
  duplicate declaration above. The fixed loading test passes.
- Host binding and automatic splitting of the real
  `Qwen3.8-27B-IQ3_S-vision-mtp-dflash2-q3.ninfer` pass with resident Vision, both
  without drafting and with DFlash2. Synthetic 40/40 GiB budgets produce 32/32 and
  31/33 splits respectively; 40/80 GiB budgets produce 23/41 and 20/44.
- Real Windows dual-GPU HTTP inference passes with resident Vision and DFlash2,
  automatic splitting (no `--stage-layers`), CUDA graphs, INT8 KV, context 8192,
  prefill chunk 128, concurrency 1, Vision maximum 64 merged tokens, KVMem budget
  1024, reserve 512 and host budget 512 MiB. The actual resolved split is 32/32.
  Independent solid-red and solid-blue 64×64 PNG requests return `Red` and `Blue`.
  Both requests have zero prompt cache reuse and execute DFlash2 verification.
- Device order is RTX 5060 Ti, then RTX 5050 Laptop. Both were idle before the run;
  their UUIDs are recorded in the local evidence and confirmed by
  `/props.execution.monitor_devices`. Reported weight
  residency is 7.36 GiB and 5.42 GiB respectively.
- The ordered patch applies to prepared tree
  `f485770ff15c81f1a82139bec85992d938d29382`, yielding
  `d976d3f8b2069f1c6255b803a70466360a107288`. All 20 ordered patches were reapplied
  from the pinned upstream with every intermediate tree and patch hash checked.

## Build and limits

Validation uses the existing Release/SM120a CUDA 13.2 build and VS2022 17.14.23,
with the pinned `ninfer-x64-windows-c7684b1d96347de8` dependency bundle verified
before building and testing. Ninja's focused dry run requested CMake regeneration
because an unrelated runtime CMake file was newer than its metadata. Instead of
reconfiguring that shared build, the validation compiled eight host translation
units with its saved commands and eight jobs, then relinked known-compatible
libraries from the same build. The host overrides include the current DFlash2 and
CPU-props changes. No CUDA kernels or existing build metadata were changed.

The pre-merge audit synchronized master `092fd8f` (the memory-planning change),
rebuilt its three affected host units (`model_instance.cpp`, `budget_plan.cpp`,
and the serving entry point), and relinked the validation server. Existing class
layouts remain compatible; the added budget API is compiled into this executable.
The synchronized server repeated the automatic 32/32 dual-GPU startup and both
image checks successfully (red and blue), with resident Vision and DFlash2.

This is a validation executable, not an installed release. Local evidence and
reproduction scripts are in `multigpu-vision-fix-20261029/`, including dependency
lock, build commands, before/after test logs, requests/responses and server log.
The first HTTP harness attempt was blocked by sandbox loopback access after the
server had initialized; rerunning with local-network access completed the tests.
The linker reports the existing unused `nvcuda.dll` delay-load warning.

This change restores loading and image execution; it does not establish a speed
improvement or broad image-quality qualification. CUDA arithmetic is not modified.
Large images, long contexts, other model families and other GPU architectures
were not qualified by this run. Overlay residency remains unsupported with a
multi-device split under the existing loader contract.
