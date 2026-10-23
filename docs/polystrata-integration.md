# PolyStrata integration

The opt-in `polystrata` backend pins upstream `v0.1.45-poly.2` at
`974552f562aecb3e86cdd2679e76e4515b52b9d2`. It supports qwen35moe GGUF models
on one CUDA GPU, with F16 or Q8_0 KV. The source submodule stays at the upstream
commit; `backends/versions.json` identifies the integration patch and resulting tree.

The backend provides bounded Query/replay, resident-only Pressure on tool
continuations, process-shared RAM/SSD history storage, native concurrent sequences,
solo embedded MTP and CPU/GPU vision. Batch execution uses ordinary decode.
See [launch and package instructions](multi-backend.md#polystrata-qwen35moe-接入2026-10-22-源码).

The mainline adaptation preserves independent scorer, graph and LRFU controls.
The console checks the native worker's capability response before launching the
Python frontend; configured replay limits reach the worker without an alias mismatch.
The frontend forwards native scoring confirmation records to its supervising console.
This change includes the existing launcher and monitor integration. The later unified
chat and console shell work is separate.

## Validation on 2026-10-23

- Reconstructed the native patch against the pinned base and verified the resulting tree.
- Built the CUDA worker and native regression targets with eight compilation jobs.
- Ran the Python console, packaging, portability, public-evidence and adapter suites;
  ran the Windows backend launcher contract and the complete PolyStrata launch-plan chain.
- Verified independent scorer/LRFU combinations and explicit Query/replay limits.
- Passed the PolyStrata browser fixtures and JavaScript reasoning/history contract.
- On RTX 5050 Laptop, passed native normalized capture, Query spans, graph positions,
  F16/Q8 page-mass references, history/session planning and shared quota checks.
- On that card, both F16 and Q8 passed separate document and question turns: the
  document reply was `Received.`, the question reused 3438 prefix tokens and returned
  the expected reference key. Requests, responses and native spans are retained locally.
- Q8 MTP accepted-prefix and cancellation checks matched serial native KV bytes,
  recurrent/hidden state and MeanK statistics in the selected one-case regression.

Prior integration validation also covered C2/C4/C8, SSD, acceptance faults and CPU/GPU
vision. Those full matrices were not all rerun for mainline adaptation. No performance
comparison was run as part of this merge validation.

## Known limits

A prior two-stage, 7131-token code-document probe answered both questions correctly
at B2048 and with original full context. B1024 answered the first correctly but
returned `Received.` for the second. Query/replay and prefix boundaries were valid;
this is evidence of a small-budget quality limitation, not a universal B2048 guarantee.
The two questions share a document and are correlated probes.

In the earlier RTX 5060 Ti fixed-output, fresh-process, no-CUDA-graph F16 comparison,
median prefill throughput was 279.8 tokens/s upstream and 259.3 tokens/s integrated
(-7.3%). Decode was 37.96 versus 37.70 tokens/s (-0.7%). The prefill difference remains
unresolved and does not establish a general throughput regression across configurations.

Multi-GPU and batch MTP are unsupported in this integration. HIP and SYCL were not
validated. SSD history is process-local. Host limits account for native KV payload,
not total process memory or every checkpoint/workspace allocation.
