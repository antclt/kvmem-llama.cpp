# SM75 T5 prefill repair — 2026-10-29

The T5 tall prefill kernel requests 68,608 bytes (67 KiB) of dynamic shared memory.
The release selected it beyond 64 input tokens for aligned, 128-row-multiple weights,
although SM75 supports only 65,536 bytes per block. Tool descriptions make prompts
long enough to reach this path; ordinary long prompts can reach it too.

## Change

`src/ops/linear/t5/t5_project.cu` now uses the existing per-device shape-set dispatch.
The tall kernel is eligible only when its shared-memory requirement fits that shape
set. SM75 uses the existing 64-by-64 GEMM, which needs 25,600 bytes and tiles arbitrary
token counts. A static assertion checks this smaller kernel against the selected
shape-set limit. Multi-architecture builds select by the current device; standard
shape sets retain the tall route. Public API, model format, workspace formula and
quantization policy do not change. This is a route repair, not a change to kernels'
arithmetic or a reduction of the tall kernel's actual allocation.

The regression adds first-use CUDA Graph capture and linear/residual checks at token
counts 63, 64, 65, 127, 128, 129, 255, 256, 257 and 513. Existing full tests also cover
split/strided GDN and attention destinations, row views, SwiGLU, embedding and top-k.
Numerical acceptance uses the existing independently decoded FP64 oracle and A8/BF16
tolerances, plus output memory guards, rather than exact old output.

## Validation

- MSVC 14.44, CUDA 13.2, Release, pinned native bundle
  `ninfer-x64-windows-c7684b1d96347de8`; dependency check passed.
- SM75 and SM120a T5 compilation passed. Four CUDA translation-unit variants and two
  host translation units were compiled with an eight-job limit. The initial sandbox
  CUDA compiler invocation failed with access violations; the same scoped build
  passed outside the sandbox.
- RTX 5060 Ti, UUID `GPU-A`: the unpatched SM75
  T5 object failed the injected 64-KiB admission guard at **68,608 bytes**, exit 86.
  The repaired SM75 T5 object and repaired SM120a T5 object both passed the full T5
  oracle suite. SM75 device code ran through PTX on this SM120 GPU.
- HTTP validation used RTX 5050 Laptop, UUID
  `GPU-B`, as the authorized idle fallback after
  the preferred dense GPU was occupied at launch. Both HTTP variants used this card.
  The model was `Bonsai2-T5-MTP-r3-Q4.ninfer`; prefill 256, context 8192, KV rk4v4,
  budget/reserve 4096/1024, concurrency 1, speculation and thinking disabled.
- Each variant passed **7/7 HTTP cases**: long ordinary chat, tools auto, tools none,
  named tool call, streamed tool call, Anthropic tools, and a final ordinary chat.
  The long prompt had 677 tokens; the auto-tools prompt had 281 tokens; both had no
  cached tokens. Named calls returned `get_weather` with parseable city arguments.
  Later streaming/Anthropic probes reused prefixes and are not separate cold-prefill
  qualifications. The qualified newer host strips tools for `tool_choice=none`,
  so that case checks API survival, not the original release's prompt rendering.
- Initial HTTP setup had a Python module-name collision and an unsupported native
  launch flag; these harness issues were corrected. Sandbox loopback connections
  failed with WinError 10013; the final HTTP runs completed outside the sandbox.
  All owned workers exited and both cards were idle after validation.
- The initial 16 integration patches were replayed and verified, then the two-file fix
  was appended as `ninfer-t5-sm75-prefill.patch`. Final prepared backend tree:
  `a2892b5e3a303e3b80b21365c4a8853777fe3874`. Source diff whitespace checks passed.

## Build and evidence boundaries

The retained build directory requested regeneration due to an unrelated edit to
`src/runtime/CMakeLists.txt`. It was not reconfigured or cleaned. The validation
build compiles only T5 and the test using recorded compiler options and relinks
against the compatible 2026-10-28 native-tool validation archives. T5's signatures
and data layouts are unchanged. Existing Ninja metadata, objects and libraries were
not modified. The old SM75 full-engine build directory is no longer present.

The SM75 validation executables contain SM75 T5 code plus previously qualified
SM120a code for the remaining engine. A test-only function enforces the 64-KiB
limit on T5 shared-memory opt-ins. This guard is absent from the production patch.
These executables are **not distributable RTX20 workers**. No physical SM75 test,
complete SM75 worker link, package replacement or release upload is claimed.
SM75 prefill performance has not been measured; the smaller tiles may cost throughput.
The implementation and patch chain are complete. Release packaging is a separate step.

Evidence and reproducible scoped commands are under
`$ARTIFACTS/sm75-t5-fix-20261029/`:
`focused-plan.json`, `build-record.json`, `build.ps1`, `gpu-results.json`,
`t5-sm75-before-guard.log`, `t5-sm75-guard.log`, `t5-sm120a.log`, and
`http-t5-sm75-guard/` / `http-t5-sm120a/` request, response and server logs.

## Integration on current master

The isolated submission was based on master `a93c337`. All 20 existing NInfer patches were replayed with their declared hashes and trees verified. The same two-file repair applies without modification; the repaired files and relevant T5/common CUDA headers match the locally validated sources. The resulting prepared tree is `71f47f722a9f23f2f0db661eb903864dfc759583` (baseline `d976d3f8b2069f1c6255b803a70466360a107288`). This reuses the scoped kernel/HTTP evidence above; it does not claim a new full-engine GPU run on this master revision.
